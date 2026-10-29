"""A job's checks over its read text run in a job child (engines.process_job), never in the API process: the
native-text gates, evidence, the cross-read guards, the printed-column flags, the AI prompt's supplier examples and
the evidence annotation. The child gets the same result process() gets in this process; a child whose own work runs
past the readers' time limit is stopped, the job ends with a plain error and a content-free audit row, and the API
answers as usual (security finding N5)."""
import json
import os
import pathlib
import sys
import time
import types

import pytest

from app import engines
from app.evidence import annotate
from app.learned import LearnedStore
from app.models import Invoice, ProcessingOptions
from tests.conftest import audit_rows, bearer
from tests.test_batch import plan, studio, upload, wait_until

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
READ = {"text": "INVOICE INV-1\nTotal 10.00\n", "boxes": [], "tables": [], "extraction_method": "template",
        "invoice": {"number": "INV-1", "supplier_name": "Harbour Lane", "currency": "AED", "date": "2026-10-01",
                    "net": "10.00", "tax": "0.50",
                    "lines": [{"sku": "A1", "description": "Pens", "qty": "2", "uom": "EA", "price": "5.00",
                               "net_amount": "10.00", "tax_amount": "0.50"}]}}
AI_READ = {"number": "INV-1", "supplier_name": "Harbour Lane Stationers", "po": "PO-77", "net": "10.00",
           "lines": [{"sku": "A1", "description": "Blue pens", "qty": "2", "price": "5.00", "net_amount": "10.00"}]}


def installed(*readers):
    return lambda: [{"id": r, "installed": r in readers, "models_blocked": False, "version": None, "note": ""}
                    for r in ("invoice2data", "paddleocr", "docling")]


def scan_read(engine, path, root, language="en", extra=()):
    scan = json.loads((FIXTURES / "invoice-scan.paddleocr.json").read_text())
    from app.ocr_worker import structured_extract
    invoice, _ = structured_extract(scan["text"], scan["boxes"], [], [])
    return {"text": scan["text"], "boxes": scan["boxes"], "tables": [], "invoice": invoice, "extraction_method": "template"}


CASES = {
    # The real invoice2data reader over the bundled PDFs: the native-text gates and the heading patterns.
    "invoice.pdf": ("auto", None, None),
    "invoice-2.pdf": ("paddleocr", None, None),
    "plain-invoice.pdf": ("auto", None, None),
    # A scan read by PaddleOCR (a recorded read), then the AI for the gaps: the scan guards and the merge.
    "invoice-scan.png": ("auto", scan_read, ("invoice2data", "paddleocr")),
}


@pytest.mark.parametrize("sample", sorted(CASES))
def test_the_job_child_returns_what_process_returns_in_this_process(sample, tmp_path, monkeypatch):
    engine, reader, readers = CASES[sample]
    if reader is not None:
        monkeypatch.setattr(engines, "local_read", reader)
    if readers is not None:
        monkeypatch.setattr(engines, "capabilities", installed(*readers))
    (tmp_path / "work").mkdir()
    store = types.SimpleNamespace(root=tmp_path)
    options = ProcessingOptions(engine=engine, ai_fallback=True, model="m")
    path = ROOT / "samples" / sample
    asked = []

    def ai_reader(path, text, options, examples=None):
        asked.append((text, examples))
        return Invoice.model_validate(AI_READ), {"evidence": {"header": {}}, "input_tokens": 3}

    def recorder(calls):
        return lambda engine, message, details=None: calls.append((engine, message, json.dumps(details, default=str)))

    here_progress, there_progress = [], []
    here = engines.process(path, options, store, ai_reader, recorder(here_progress))
    here_asked = list(asked);asked.clear()
    there = engines.process_job(path, options, store, ai_reader, recorder(there_progress),
                                learned=LearnedStore(tmp_path), filename="INV-1.pdf")
    annotated = there.pop("annotated_evidence")
    # As the job store keeps them (JSON), less the time each step took.
    untimed = lambda value: ({k: untimed(v) for k, v in value.items() if not k.endswith("seconds")} if isinstance(value, dict)
                             else [untimed(v) for v in value] if isinstance(value, list) else value)
    strip = lambda result: untimed(json.loads(json.dumps(result, default=str)))
    assert strip(there) == strip(here)
    assert annotated == annotate(here["evidence"], here["invoice"], filename="INV-1.pdf")
    assert [(e, m) for e, m, _ in there_progress] == [(e, m) for e, m, _ in here_progress]
    # The child matched the supplier examples (none learned here) and sent them with the same text.
    assert [text for text, _ in asked] == [text for text, _ in here_asked] and all(x == "" for _, x in asked)
    assert list((tmp_path / "work").iterdir()) == []


def stubbed_job(tmp_path, monkeypatch, stub, **env):
    """Upload one invoice whose job child starts with `stub` run first; the readers are answered at once here.

    Returns the app, the client, its token, the job id and the file the child's pid is written to."""
    pid_file = tmp_path / "job.pid"
    shim = tmp_path / "stubbed-python"
    shim.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nshift 2\n"
                    f"exec {sys.executable} -c \"{stub}; import runpy; "
                    f"runpy.run_module('app.job_worker', run_name='__main__', alter_sys=True)\" \"$@\"\n")
    shim.chmod(0o700)
    monkeypatch.setattr(engines.sys, "executable", str(shim))
    monkeypatch.setattr(engines, "local_read", lambda *a, **k: json.loads(json.dumps(READ)))
    monkeypatch.setattr(engines, "capabilities", installed("invoice2data"))
    app, client, token = studio(tmp_path, monkeypatch, engines.process_job, **env)
    plan_token = plan(client, token, ["stubbed.txt"])
    jid = upload(client, token, plan_token, "stubbed.txt").json()["id"]
    return app, client, token, jid, pid_file


def assert_ended(app, jid, pid_file, error_type):
    """The job ended in error with a content-free audit row, its child is gone and the slots are free."""
    assert wait_until(lambda: app.state.store.job(jid)["status"] == "error", timeout=30)
    assert app.state.store.job(jid)["error"].startswith(f"Processing failed ({error_type}).")
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    rows = [json.loads(payload) for event, payload in audit_rows(app) if event == "extraction_failed"]
    # The row names the job and the error type only: no document content.
    assert [{k: v for k, v in row.items() if k != "actor"} for row in rows] == [
        {"job_id": jid, "error_type": error_type, "approved": False}]
    assert engines._LOCAL_OCR_SLOTS.acquire(blocking=False) and engines._AI_SLOTS.acquire(blocking=False)
    engines._LOCAL_OCR_SLOTS.release();engines._AI_SLOTS.release()


def test_a_job_whose_checks_never_finish_ends_with_an_error_and_the_api_keeps_answering(tmp_path, monkeypatch):
    # A stubbed post-read step that never finishes, in the job child.
    stall = "import time; import app.engines as e; e.guard_header = lambda *a, **k: time.sleep(600)"
    started = time.monotonic()
    app, client, token, jid, pid_file = stubbed_job(tmp_path, monkeypatch, stall, INV_ENGINE_TIMEOUT="2")
    assert wait_until(lambda: pid_file.exists(), timeout=20)
    # The worker is busy with the stalled job; the API answers other requests meanwhile.
    asked = time.monotonic()
    assert client.get("/api/queue", headers=bearer(token)).status_code == 200
    assert time.monotonic() - asked < 2
    assert_ended(app, jid, pid_file, "ChecksTimedOut")
    assert time.monotonic() - started < 25


PADDED = "'x' * 3 * 1024 * 1024"
OVERSIZED = {
    # The final message: the result.
    "done": f"e.process = lambda *a, **k: {{**process(*a, **k), 'pad': {PADDED}}}",
    # A call before it: a progress note, then the work carries on.
    "call": f"e.process = lambda path, options, store, ai, progress: (progress('auto', {PADDED}), process(path, options, store, ai, progress))[1]",
}


@pytest.mark.parametrize("message", sorted(OVERSIZED))
def test_a_message_over_the_size_limit_stops_the_child_and_ends_the_job(message, tmp_path, monkeypatch):
    # A child whose message is padded past the cap: the API process stops reading at the cap. (The stub patches the
    # engines module the child's __main__ shares; runpy runs app.job_worker afresh, so patching it would not hold.)
    monkeypatch.setattr(engines, "JOB_MESSAGE_MB", 1)
    pad = f"import app.engines as e; process = e.process; {OVERSIZED[message]}"
    app, client, token, jid, pid_file = stubbed_job(tmp_path, monkeypatch, pad)
    assert_ended(app, jid, pid_file, "ReaderFailed")
    assert client.get("/api/queue", headers=bearer(token)).status_code == 200


@pytest.mark.parametrize("value", ["", "0", "-5", "none", " "])
def test_the_message_cap_cannot_be_switched_off(value):
    assert engines._job_message_mb(value) == 64
    assert engines._job_message_mb("8") == 8


def test_a_second_acquire_of_a_held_slot_is_refused_and_ends_the_job(tmp_path, monkeypatch):
    # A post-read step that asks for the AI slot twice: refused with an error, not a wait on itself.
    twice = "import app.engines as e; e.guard_header = lambda *a, **k: (e._AI_SLOTS.acquire(), e._AI_SLOTS.acquire())"
    app, client, token, jid, pid_file = stubbed_job(tmp_path, monkeypatch, twice, INV_ENGINE_TIMEOUT="20")
    assert_ended(app, jid, pid_file, "ReaderFailed")
    assert client.get("/api/queue", headers=bearer(token)).status_code == 200
