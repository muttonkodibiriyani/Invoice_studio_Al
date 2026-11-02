"""The deployed-app runner (scripts/verify_deployed.py) against the real app in cloud mode.

The app, its sign-in, preflight, upload, job queue and delete are the real ones (conftest's stand-in Entra tenant);
only the reader is replaced, by a fake whose answer each test chooses, so every verdict here is known in advance. The
tests check the runner never calls a wrong, missing, AI-helped or failed read a proved one.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

from app import main as main_module
from app.main import create_app
from app.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_deployed as check  # noqa: E402
import engine_scorecard as scorecard  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
EXPECTED = json.loads((SAMPLES / "invoice.json").read_text(encoding="utf-8"))
NAMES = list(scorecard.ENGINES) + [scorecard.CHAIN]
INSTALLED = [{"id": e, "installed": True, "models_blocked": False, "version": "1", "note": "Ready"}
             for e in scorecard.ENGINES]


class BlobStore(Store):
    """The SQLite store with the cloud store's blob hooks as no-ops (as in test_batch): uploads stay on local disk."""

    def persist_blob(self, path):
        pass

    def ensure_blob(self, path):
        pass


@pytest.fixture
def app(tmp_path, identity):
    return create_app(tmp_path / "data", identity=identity, store_type=BlobStore)


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    shutil.copy(SAMPLES / "invoice.pdf", root / "invoice.pdf")
    shutil.copy(SAMPLES / "invoice.json", root / "invoice.json")
    return root


class Reader:
    """Stands in for process_job: answers per engine, and records the options each job was read with."""

    def __init__(self, monkeypatch, read=None, raises=None, ai=False):
        self.seen = []
        self.read = read or (lambda engine: json.loads(json.dumps(EXPECTED)))
        self.raises, self.ai = raises, ai
        monkeypatch.setattr(main_module, "process_job", self)
        monkeypatch.setattr(main_module, "capabilities", lambda: INSTALLED)

    def __call__(self, path, options, store, ai_reader, progress=None, learned=None, filename=None):
        self.seen.append(options.model_dump())
        if self.raises:
            raise self.raises
        engine = "invoice2data" if options.engine == "auto" else options.engine
        trace = [{"engine": engine, "status": "extracted"}]
        if self.ai:
            trace.append({"engine": "openai", "model": "m", "status": "extracted", "role": "fallback"})
        invoice = self.read(options.engine)
        return {"invoice": invoice, "selected_engine": engine if invoice else "none", "trace": trace, "text": "x" * 50,
                "boxes": [], "evidence": {}, "annotated_evidence": {},
                "readers": {"ai": {"status": "fallback" if self.ai else "off", "need": "cross_check"},
                            "header": {}, "lines": [], "gaps": [], "not_installed": []}}


@pytest.fixture
def signed_in(client, owner):
    client.headers["Authorization"] = "Bearer " + owner
    return client


def run(client, corpus, tmp_path, *extra):
    out = tmp_path / "out"
    code = check.main([str(corpus), "--out", str(out), "--job-timeout", "60", "--poll", "0.05", *extra], client=client)
    report = json.loads((out / "verify-deployed.json").read_text(encoding="utf-8")) if out.exists() else None
    return code, report


def local_from(report):
    """A local scorecard.json that agrees with a deployed report case for case."""
    return {"target": "local", "cases": report["cases"], "summary": report["summary"]}


def test_correct_reads_by_every_reader_are_proved_and_cleaned_up(signed_in, corpus, tmp_path, monkeypatch):
    reader = Reader(monkeypatch)
    code, report = run(signed_in, corpus, tmp_path)
    assert code == 0
    assert {n: v["verdict"] for n, v in report["verdicts"].items()} == {n: "proved" for n in NAMES}
    assert all(report["summary"][n]["accuracy"] == 1.0 and report["summary"][n]["exact"] == 1 for n in NAMES)
    # The scorecard's own options, AI off on every run, one upload per reader.
    assert [(o["engine"], o["prefer_native_text"], o["ai_fallback"]) for o in reader.seen] == [
        ("invoice2data", False, False), ("paddleocr", False, False), ("docling", False, False), ("auto", True, False)]
    assert report["clean_up"] == {"uploaded": 4, "deleted": 4, "left": 0, "why": None}
    assert signed_in.get("/api/state", headers=check.HEADERS).json()["jobs"] == []


def test_one_cent_off_is_scored_wrong_and_differs_from_a_correct_local_run(signed_in, corpus, tmp_path, monkeypatch):
    Reader(monkeypatch)
    _, good = run(signed_in, corpus, tmp_path)
    (tmp_path / "local.json").write_text(json.dumps(local_from(good)), encoding="utf-8")
    shutil.rmtree(tmp_path / "out")

    def cent_off(engine):
        invoice = json.loads(json.dumps(EXPECTED))
        if engine == "docling":
            invoice["net"] = "800.01"
        return invoice
    Reader(monkeypatch, read=cent_off)
    code, report = run(signed_in, corpus, tmp_path, "--local", str(tmp_path / "local.json"))
    assert code == 1
    assert report["summary"]["docling"]["fields"]["net"]["wrong"] == 1
    assert report["differences"] == {"docling": {"invoice.pdf": ["fields", "exact"]}}
    assert report["verdicts"]["docling"]["verdict"] == "not_proved"
    assert all(report["verdicts"][n]["verdict"] == "proved" for n in NAMES if n != "docling")


def test_a_matching_local_scorecard_proves_every_reader(signed_in, corpus, tmp_path, monkeypatch):
    Reader(monkeypatch)
    _, first = run(signed_in, corpus, tmp_path)
    (tmp_path / "local.json").write_text(json.dumps(local_from(first)), encoding="utf-8")
    code, report = run(signed_in, corpus, tmp_path, "--local", str(tmp_path / "local.json"))
    assert code == 0 and report["differences"] == {}
    assert all(v["compared_with_local"] for v in report["verdicts"].values())


def test_a_read_the_ai_helped_is_never_scored_or_proved(signed_in, corpus, tmp_path, monkeypatch):
    Reader(monkeypatch, ai=True)
    code, report = run(signed_in, corpus, tmp_path)
    assert code == 1
    for name in NAMES:
        assert report["summary"][name]["read"] == 0 and report["summary"][name]["statuses"] == {"failed": 1}
        assert report["cases"][name]["invoice.pdf"]["detail"] == ["ai_called"]
        assert report["verdicts"][name]["verdict"] == "not_proved"
    assert report["ai_called"] == {n: ["invoice.pdf"] for n in NAMES}


def test_ai_status_alone_counts_as_an_ai_call():
    job = {"trace": [{"engine": "docling", "status": "extracted"}, {"engine": "guard_printed", "status": "x"},
                     {"engine": "openai", "status": "skipped"}], "readers": {"ai": {"status": "gap_fill"}}}
    assert check.ai_calls(job)
    job["readers"]["ai"]["status"] = "off"
    assert check.ai_calls(job) == []


def test_a_failed_job_is_crashed_with_its_class_not_a_zero_score(signed_in, corpus, tmp_path, monkeypatch):
    Reader(monkeypatch, raises=TimeoutError("the document said: SECRET-SUPPLIER"))
    code, report = run(signed_in, corpus, tmp_path)
    assert code == 1
    case = report["cases"]["docling"]["invoice.pdf"]
    assert case["status"] == "crashed" and "TimeoutError" in case["detail"] and "fields" not in case
    assert report["summary"]["docling"]["accuracy"] is None
    assert "SECRET-SUPPLIER" not in (tmp_path / "out" / "verify-deployed.json").read_text(encoding="utf-8")


def test_no_invoice_content_leaves_in_the_output(signed_in, corpus, tmp_path, monkeypatch):
    Reader(monkeypatch)
    run(signed_in, corpus, tmp_path)
    for name in ("verify-deployed.json", "verify-deployed.txt"):
        written = (tmp_path / "out" / name).read_text(encoding="utf-8")
        for value in ("Lumena", "DEMO-2026-001", "LUM-SER30", "00012345678905"):
            assert value not in written, (name, value)


def test_a_reader_missing_from_the_container_is_not_proved(signed_in, corpus, tmp_path, monkeypatch):
    Reader(monkeypatch)
    blocked = [dict(c, models_blocked=c["id"] == "paddleocr", installed=c["id"] != "docling") for c in INSTALLED]
    monkeypatch.setattr(main_module, "capabilities", lambda: blocked)
    code, report = run(signed_in, corpus, tmp_path)
    assert code == 1
    assert report["verdicts"]["docling"]["why"] == ["not installed in the deployed app"]
    assert report["verdicts"]["paddleocr"]["why"] == ["installed, but its models are blocked in the deployed app"]
    assert report["verdicts"]["invoice2data"]["verdict"] == "proved"


def test_a_reader_that_reads_nothing_is_not_proved(signed_in, corpus, tmp_path, monkeypatch):
    Reader(monkeypatch, read=lambda engine: {} if engine == "paddleocr" else json.loads(json.dumps(EXPECTED)))
    code, report = run(signed_in, corpus, tmp_path)
    assert code == 1
    assert report["cases"]["paddleocr"]["invoice.pdf"]["status"] == "no_invoice"
    assert "read no case" in report["verdicts"]["paddleocr"]["why"]


def test_a_refused_token_stops_before_any_upload(client, corpus, tmp_path, monkeypatch):
    reader = Reader(monkeypatch)
    client.headers["Authorization"] = "Bearer not-a-token"
    code, report = run(client, corpus, tmp_path)
    assert code == 2 and report is None and reader.seen == []


def test_a_member_cannot_delete_so_the_jobs_left_are_counted(tenant, client, owner, corpus, tmp_path, monkeypatch):
    Reader(monkeypatch)
    member = tenant.token("member-uid", "member@example.test", roles=("member",))
    client.headers["Authorization"] = "Bearer " + member
    code, report = run(client, corpus, tmp_path)
    assert code == 0
    assert report["clean_up"] == {"uploaded": 4, "deleted": 0, "left": 4, "why": "deleting needs the admin app role"}


def test_keep_jobs_leaves_them(signed_in, corpus, tmp_path, monkeypatch):
    Reader(monkeypatch)
    _, report = run(signed_in, corpus, tmp_path, "--keep-jobs", "--engines", "docling", "--no-chain")
    assert report["clean_up"] == {"uploaded": 1, "deleted": 0, "left": 1, "why": "--keep-jobs"}
    assert len(signed_in.get("/api/state", headers=check.HEADERS).json()["jobs"]) == 1


def test_a_job_that_never_finishes_is_timed_out_not_scored():
    class Stuck:
        def __init__(self):
            self.now = 0.0

        def post(self, path, headers=None, **kwargs):
            body = {"/api/preflight": {"token": "t", "blocking": []}, "/api/preflight/confirm": {"confirmed": True},
                    "/api/invoices": {"id": "j", "status": "queued", "revision": 1}}[path]
            return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: body})()

        def get(self, path, headers=None):
            return type("R", (), {"raise_for_status": lambda self: None,
                                  "json": lambda self: {"id": "j", "status": "processing", "revision": 1}})()

    stuck = Stuck()

    def sleep(seconds):
        stuck.now += seconds
    runner = check.Deployed(stuck, poll=5, job_timeout=30, clock=lambda: stuck.now, sleep=sleep)
    run_ = runner.read(SAMPLES / "invoice.pdf", "docling")
    assert run_["status"] == "timed_out" and run_["detail"] == ["job still processing"]
    assert runner.created == [["j", 1]]


def test_compare_names_a_case_only_one_side_ran():
    deployed = {"cases": {"docling": {"a.pdf": {"status": "read"}, "b.pdf": {"status": "read"}}}}
    local = {"cases": {"docling": {"a.pdf": {"status": "read"}, "c.pdf": {"status": "read"}}}}
    assert check.compare(deployed, local) == {"docling": {"b.pdf": ["only deployed"], "c.pdf": ["only local"]}}
    assert check.compare(deployed, {"cases": {}}) == {"docling": {"*": ["not in the local scorecard"]}}


def test_a_timed_out_chain_run_still_summarises_without_a_winner():
    run_ = check.job_run({"status": "queued"}, scorecard.CHAIN, 30.0)
    summary = scorecard.summarise(scorecard.CHAIN, {"a.pdf": scorecard.score_run(run_, EXPECTED)})
    assert summary["statuses"] == {"timed_out": 1} and summary["read"] == 0
    assert summary["ai_need"] == {"not_finished": 1} and summary["handover"]["count"] == 0
