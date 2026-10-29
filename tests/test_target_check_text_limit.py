"""The target check's printed-text checks run patterns over the document text. They run in a reader child under the
readers' time limit when the job is processed, are stored with the job, and review views and saves read only the
stored values: a check that runs past the limit is stopped, the view still answers with those checks marked
unavailable, and the audit row holds no document content (security finding N5)."""
import json
import os
import pathlib
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app import engines
from app import target_check as tc
from app.main import create_app
from app.store import Store
from tests.conftest import HEADERS, audit_rows
from tests.test_target_check import TEXT, line, view

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
SCAN = json.loads((FIXTURES / "plain-invoice-scan.paddleocr.json").read_text())
INVOICE = {"number": "HLS-24017", "net": "621.00", "tax": "31.05", "lines": []}


def test_the_text_checks_run_in_a_reader_child_and_check_what_the_text_checks_in_process(tmp_path):
    (tmp_path / "work").mkdir()
    for text in (TEXT, SCAN["text"], TEXT + "Discount 20.00\nTotal lines: 3\n", ""):
        checks = tc.TextChecks.validated(engines.text_checks(text, tmp_path))
        assert checks == tc.text_checks(text)
        stored, local = tc.Sources(text=tc.TextChecks(checks)), tc.Sources(text=text)
        for lines in ((), [line()], [line(), line(2)]):
            assert (tc.check_view(view(lines), stored, now=tc.datetime(2031, 1, 1)) ==
                    tc.check_view(view(lines), local, now=tc.datetime(2031, 1, 1)))
    assert list((tmp_path / "work").iterdir()) == [], "the stored text is removed once it is checked"


@pytest.mark.parametrize("data", [
    None, [], {"has_text": "yes"}, {"has_text": False, "norm": ""},
    {**tc.text_checks(TEXT), "extra": 1}, {**tc.text_checks(TEXT), "norm": 1},
    {**tc.text_checks(TEXT), "line_count": "3"}, {**tc.text_checks(TEXT), "line_count": True},
    {**tc.text_checks(TEXT), "amounts": ["x"]}, {**tc.text_checks(TEXT), "numbers": ["NaN"]},
    {**tc.text_checks(TEXT), "discounts": "1"},
])
def test_only_the_structure_the_child_returns_is_accepted(data):
    with pytest.raises(ValueError):
        tc.TextChecks.validated(data)


def test_unavailable_text_checks_leave_printed_cells_unverifiable_and_the_gross_check_skipped():
    result = tc.check_view(view(), tc.Sources(text=tc.TextChecks({"unavailable": "ReaderTimedOut"})))
    assert result["text_checks"] == "unavailable"
    gross = next(c for c in result["checks"] if c["check"] == "net_plus_tax_gross")
    assert gross["status"] == tc.SKIPPED and gross["unavailable"] and gross["detail"] == tc.UNAVAILABLE_DETAIL
    assert result["counts"][tc.VERIFIED] < tc.check_view(view(), tc.Sources(text=TEXT))["counts"][tc.VERIFIED]


def test_a_text_check_past_the_time_limit_is_stopped_and_the_review_view_still_answers(tmp_path, monkeypatch):
    # A stubbed text check that never finishes, in the reader child the stub reader starts. In this process the
    # stub fails the request instead, so the test also shows the API process never runs it.
    stall = "import time; import app.target_check as tc; tc.text_checks = lambda *a, **k: [time.sleep(1) for _ in iter(int, 1)]"
    monkeypatch.setattr(tc, "text_checks", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran in the API process")))
    pid_file = tmp_path / "reader.pid"
    reader = tmp_path / "slow-reader"
    reader.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nshift 2\n"
                      f"exec {sys.executable} -c \"{stall}; import runpy; "
                      f"runpy.run_module('app.ocr_worker', run_name='__main__', alter_sys=True)\" \"$@\"\n")
    reader.chmod(0o700)
    monkeypatch.setattr(engines.sys, "executable", str(reader))
    monkeypatch.setenv("INV_ENGINE_TIMEOUT", "1")
    monkeypatch.delenv("INV_STUDIO_CLOUD", raising=False)
    monkeypatch.delenv("INV_STUDIO_DEMO_REFERENCES", raising=False)
    app = create_app(tmp_path / "data", store_type=Store)
    app.state.store.job("old", {"id": "old", "revision": 1, "status": "review", "filename": "a.pdf",
                                "text": SCAN["text"], "boxes": SCAN["boxes"], "tables": [], "invoice": INVOICE})
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        started = time.monotonic()
        response = c.get("/api/jobs/old", headers=HEADERS)
        elapsed = time.monotonic() - started
        assert response.status_code == 200 and elapsed < 6
        check = response.json()["rules"]["target_check"]
        assert check["text_checks"] == "unavailable"
        assert next(x for x in check["checks"] if x["check"] == "net_plus_tax_gross")["unavailable"]
        assert "text_checks" not in response.json(), "the stored checks are never served"
        pid = int(pid_file.read_text())
        try:
            os.kill(pid, 0)
            raise AssertionError("the reader is still running")
        except ProcessLookupError:
            pass
        # Stored as unavailable for this text: a later view does not start the child again.
        pid_file.unlink()
        stored = app.state.store.job("old")
        assert stored["text_checks"]["unavailable"] == "ReaderTimedOut"
        stored["rules"] = None
        app.state.store.job("old", stored)
        assert c.get("/api/jobs/old", headers=HEADERS).status_code == 200 and not pid_file.exists()
    rows = [(event, json.loads(payload)) for event, payload in audit_rows(app) if event == "target_text_checks_failed"]
    # The row names the error type only: no document content.
    assert rows == [("target_text_checks_failed", {"job_id": "old", "error_type": "ReaderTimedOut",
                                                   "actor": "local-operator"})]
