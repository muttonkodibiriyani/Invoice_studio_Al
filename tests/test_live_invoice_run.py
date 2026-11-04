"""The live invoice run (scripts/live_invoice_run.py) against the real app in cloud mode, the reader faked as in
test_verify_deployed: production options, AI calls counted not failed, an expected JSON scored when present."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import live_invoice_run as live  # noqa: E402
from tests import test_verify_deployed as verify  # noqa: E402

SAMPLES, Reader = verify.SAMPLES, verify.Reader
app, signed_in = verify.app, verify.signed_in  # fixtures: the real app in cloud mode, signed in as owner


def run(client, root, tmp_path, *extra):
    out = tmp_path / "out"
    code = live.main([str(root), "--out", str(out), "--job-timeout", "60", "--poll", "0.05", *extra], client=client)
    return code, json.loads((out / "live-run.json").read_text(encoding="utf-8")) if out.exists() else None


def test_invoices_run_with_the_ai_on_and_calls_are_counted(signed_in, tmp_path, monkeypatch):
    root = tmp_path / "real"
    root.mkdir()
    shutil.copy(SAMPLES / "invoice.pdf", root / "a.pdf")
    shutil.copy(SAMPLES / "invoice.json", root / "a.json")
    shutil.copy(SAMPLES / "invoice-2.pdf", root / "b.pdf")  # no expected JSON: recorded, not scored
    reader = Reader(monkeypatch, ai=True)
    code, report = run(signed_in, root, tmp_path, "--clean-up")
    assert code == 0
    assert [(o["engine"], o["prefer_native_text"], o["ai_fallback"]) for o in reader.seen] == [("auto", True, True)] * 2
    a, b = report["invoices"]["a.pdf"], report["invoices"]["b.pdf"]
    assert a["status"] in live.DONE and a["ai_calls"] == 1 and a["score"]["exact"]
    assert "score" not in b
    assert report["totals"]["ai_calls"] == 2 and report["totals"]["ai_invoices"] == 2
    assert report["clean_up"]["deleted"] == 2


def test_a_failed_read_is_an_error_and_the_exit_code_says_so(signed_in, tmp_path, monkeypatch):
    root = tmp_path / "real"
    root.mkdir()
    shutil.copy(SAMPLES / "invoice.pdf", root / "a.pdf")
    Reader(monkeypatch, raises=RuntimeError("secret invoice text"))
    code, report = run(signed_in, root, tmp_path, "--redact-names")
    assert code == 1
    assert report["invoices"]["invoice-001"]["status"] == "error"
    assert "secret" not in json.dumps(report)
    assert report["clean_up"]["left"] == 1


def test_an_empty_directory_is_exit_2(signed_in, tmp_path):
    (tmp_path / "empty").mkdir()
    assert live.main([str(tmp_path / "empty")], client=signed_in) == 2
