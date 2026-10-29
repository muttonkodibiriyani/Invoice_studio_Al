"""The upload's multi-invoice check (pdfplumber and the page-text patterns) runs in a reader child under the readers'
time limit, never in the API process: a check that runs past the limit is stopped and the upload is refused with a
plain message and a content-free audit row (security findings N5 and M4)."""
import hashlib
import io
import json
import os
import sys
import time

import pytest

from app import engines, multi_invoice
from app.multi_invoice import analyse_pdf, outcome_from, refusal_message
from tests.conftest import audit_rows, bearer
from tests.test_batch import OPTIONS, FakeReader, studio


def pdf(*pages):
    """One page per entry; each entry is the lines printed at the top of that page."""
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(595, 842))
    for lines in pages:
        for n, line in enumerate(lines):
            page.drawString(40, 800 - 14 * n, line)
        page.showPage()
    page.save()
    return buffer.getvalue()


BOUND = pdf(["TAX INVOICE # ZZTI26-00000101", "Page 1 of 2"], ["continued", "Page 2 of 2"],
            ["TAX INVOICE # ZZTI26-00000102", "Page 1 of 1"])
SAMPLES = [
    BOUND,
    pdf(["TAX INVOICE # ZZTI26-00000201", "TAX INVOICE # ZZTI26-00000202"]),  # refused: two numbers on one page
    pdf(["Tax Invoice 1000000001", "Page 1 of 2"], ["Tax Invoice 1000000001", "Page 2 of 2"]),  # one invoice
    pdf(["Invoice Number", "Synthetic label row", "900000000101"], ["Invoice Number", "Synthetic label row", "900000000102"]),
]


@pytest.mark.parametrize("content", SAMPLES)
def test_the_check_in_the_reader_child_answers_what_it_answered_in_this_process(tmp_path, content):
    (tmp_path / "work").mkdir()
    assert outcome_from(engines.split_check(content, tmp_path)) == analyse_pdf(content)
    assert list((tmp_path / "work").iterdir()) == [], "the upload's work file is removed once it is checked"


@pytest.mark.parametrize("answer", [
    {"numbers": ["A1234", "A1234"], "segments": [], "refusal": None},
    {"numbers": ["A1234\n"], "segments": [], "refusal": None},
    {"numbers": ["A1234"], "segments": [{"number": "B1234", "pages": [1]}], "refusal": None},
    {"numbers": ["A1234"], "segments": [{"number": "A1234", "pages": [0]}], "refusal": None},
    {"numbers": ["A1234"], "segments": [{"number": "A1234", "pages": [True]}], "refusal": None},
    {"numbers": ["A1234", "B1234"], "segments": [], "refusal": "Any other text"},
    {"numbers": ["A1234"], "segments": []},
])
def test_an_answer_the_check_could_not_have_given_is_not_used(answer):
    with pytest.raises((ValueError, KeyError)):
        outcome_from(answer)
    assert outcome_from({"numbers": ["A1234", "B1234"], "segments": [],
                         "refusal": refusal_message(["A1234", "B1234"])}).refusal


def poison(monkeypatch):
    """The check fails the test if it runs in this process."""
    in_process = []

    def record(*args, **kwargs):
        in_process.append(True)
        raise RuntimeError("the split check ran inside the API process")
    monkeypatch.setattr(multi_invoice, "pdf_pages", record)
    return in_process


def upload(client, token, name, content):
    files = [{"name": name, "size": len(content)}]
    plan = client.post("/api/preflight", json={"options": OPTIONS, "files": files}, headers=bearer(token)).json()["token"]
    assert client.post("/api/preflight/confirm", json={"token": plan}, headers=bearer(token)).status_code == 200
    return client.post("/api/invoices", files={"file": (name, content, "application/pdf")},
                       data={"options": json.dumps(OPTIONS), "preflight_token": plan}, headers=bearer(token))


def test_a_bound_pdf_is_split_by_the_check_in_the_reader_child(tmp_path, monkeypatch):
    in_process = poison(monkeypatch)
    app, client, token = studio(tmp_path, monkeypatch, FakeReader())
    response = upload(client, token, "bound.pdf", BOUND)
    assert response.status_code == 200, response.text
    jobs = sorted((j for j in client.get("/api/state", headers=bearer(token)).json()["jobs"] if j.get("split")),
                  key=lambda j: j["split"]["part"])
    assert [(j["filename"], j["split"]["pages"]) for j in jobs] == [
        ("bound [1 of 2 ZZTI26-00000101].pdf", [1, 2]), ("bound [2 of 2 ZZTI26-00000102].pdf", [3])]
    assert in_process == []


def test_a_check_past_the_time_limit_is_stopped_and_the_upload_is_refused(tmp_path, monkeypatch):
    in_process = poison(monkeypatch)
    # A stubbed analysis step that never finishes, in the reader child. The upload check (app.upload_check) runs as usual.
    stall = "import time; import app.multi_invoice as m; m.pdf_pages = lambda *a, **k: time.sleep(10**6)"
    pid_file = tmp_path / "reader.pid"
    reader = tmp_path / "slow-reader"
    reader.write_text(f"#!/bin/sh\n[ \"$2\" = app.ocr_worker ] || exec {sys.executable} \"$@\"\n"
                      f"echo $$ > {pid_file}\nshift 2\n"
                      f"exec {sys.executable} -c \"{stall}; import runpy; "
                      f"runpy.run_module('app.ocr_worker', run_name='__main__', alter_sys=True)\" \"$@\"\n")
    reader.chmod(0o700)
    monkeypatch.setattr(engines.sys, "executable", str(reader))
    app, client, token = studio(tmp_path, monkeypatch, FakeReader(), INV_ENGINE_TIMEOUT="1")
    started = time.monotonic()
    response = upload(client, token, "bound.pdf", BOUND)
    assert time.monotonic() - started < 30
    assert response.status_code == 400
    assert response.json()["detail"].startswith("This PDF could not be checked for several invoices")
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert client.get("/api/state", headers=bearer(token)).json()["jobs"] == []
    rows = [(event, json.loads(payload)) for event, payload in audit_rows(app) if event == "upload_refused"]
    # The row names the stage, the error type and the file's hash only: no document content.
    assert [(event, {k: v for k, v in payload.items() if k != "actor"}) for event, payload in rows] == [
        ("upload_refused", {"stage": "split_check", "error_type": "ReaderTimedOut",
                            "sha256": hashlib.sha256(BOUND).hexdigest()})]
    assert in_process == []
