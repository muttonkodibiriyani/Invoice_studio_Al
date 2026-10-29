"""Learning from an exported invoice reads its stored text again and learns its supplier's template from it. Both run
in a reader child under the readers' time limit, never in the API process, which only writes the learned files the
child hands back: a child that runs past the limit is stopped, the export's learning step is recorded as failed at
that stage, and the API answers as usual (security finding N5)."""
import json
import os
import pathlib
import re
import sys
import threading
import time

import pytest

from fastapi.testclient import TestClient

from app import engines
from app import learned as learned_module
from app.learned import LearnedStore
from app.main import create_app
from app.ocr_worker import structured_extract
from app.store import Store
from tests.conftest import HEADERS, audit_rows

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
SCAN = json.loads((FIXTURES / "plain-invoice-scan.paddleocr.json").read_text())


def verified(text, boxes):
    invoice, _ = structured_extract(text, boxes, [], [])
    return invoice


def files(root):
    return {path.relative_to(root).as_posix(): path.read_text()
            for path in sorted((root / "learned").rglob("*")) if path.is_file()}


def test_learning_in_the_reader_child_writes_what_learning_in_this_process_wrote(tmp_path):
    invoice = verified(SCAN["text"], SCAN["boxes"])
    here, there = tmp_path / "here", tmp_path / "there"
    for root in (here, there):
        (root / "work").mkdir(parents=True)
    expected = LearnedStore(here).record_verified(invoice, SCAN["text"], "job-1", "export",
                                                  verified(SCAN["text"], SCAN["boxes"]))
    record = engines.stored_learn(SCAN["text"], SCAN["boxes"], [], invoice, "job-1", there)
    assert files(there) == {}, "the child writes nothing"
    LearnedStore(there).apply(record["writes"])
    assert record["outcome"]["template"] == "learned" and record["outcome"] == expected
    # recorded_at and learned_at may fall in different seconds.
    strip = lambda found: {name: re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", "", body) for name, body in found.items()}
    assert strip(files(there)) == strip(files(here))
    assert list((there / "work").iterdir()) == [], "the stored read is removed once it is learned"
    # A second export of the same supplier runs the template's reproduce check in the child too.
    again = engines.stored_learn(SCAN["text"], SCAN["boxes"], [], invoice, "job-2", there)
    assert again["outcome"]["template"] == "confirmed"


def test_an_export_learns_its_supplier_through_the_reader_child(tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_LEARN", "1")
    app = create_app(tmp_path / "data", store_type=Store)
    app.state.store.job("done", {"id": "done", "revision": 2, "status": "exported", "text": SCAN["text"],
                                 "boxes": SCAN["boxes"], "tables": [], "invoice": verified(SCAN["text"], SCAN["boxes"])})
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        response = c.post("/api/learned/jobs/done", headers=HEADERS)
    assert response.status_code == 200
    assert [(item["supplier_key"], item["template"]) for item in response.json()["suppliers"]] == [
        ("harbour-lane-stationers-llc", "learned")]


@pytest.mark.parametrize("name", ["../users.json", "templates/../../x.yml", "suppliers/a/b.json", "corrections.json"])
def test_the_api_process_writes_only_supplier_and_template_files_a_child_hands_back(tmp_path, name):
    with pytest.raises(ValueError):
        LearnedStore(tmp_path).apply([(name, "{}")])
    assert not (tmp_path / "users.json").exists() and not (tmp_path.parent / "x.yml").exists()


def test_a_learning_step_past_the_time_limit_is_stopped_and_the_learning_step_fails_alone(tmp_path, monkeypatch):
    # A stubbed learning step that outlasts the limit, wherever it runs: in this process or in the reader child,
    # which the stub reader starts with the same stub in place.
    stall = "import time; import app.learned as learned; learned.learn_template = lambda *a, **k: time.sleep(8)"
    monkeypatch.setattr(learned_module, "learn_template", lambda *a, **k: time.sleep(8))
    pid_file = tmp_path / "reader.pid"
    reader = tmp_path / "slow-reader"
    reader.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nshift 2\n"
                      f"exec {sys.executable} -c \"{stall}; import runpy; "
                      f"runpy.run_module('app.ocr_worker', run_name='__main__', alter_sys=True)\" \"$@\"\n")
    reader.chmod(0o700)
    monkeypatch.setattr(engines.sys, "executable", str(reader))
    monkeypatch.setenv("INV_ENGINE_TIMEOUT", "1")
    monkeypatch.setenv("INV_STUDIO_LEARN", "1")
    monkeypatch.delenv("INV_STUDIO_CLOUD", raising=False)
    app = create_app(tmp_path / "data", store_type=Store)
    app.state.store.job("done", {"id": "done", "revision": 2, "status": "exported", "text": SCAN["text"],
                                 "boxes": SCAN["boxes"], "tables": [], "invoice": verified(SCAN["text"], SCAN["boxes"])})
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        started = time.monotonic()
        response = c.post("/api/learned/jobs/done", headers=HEADERS)
        elapsed = time.monotonic() - started
    assert response.status_code == 200 and elapsed < 6
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, 0)
        raise AssertionError("the reader is still running")
    except ProcessLookupError:
        pass
    rows = [(event, json.loads(payload)) for event, payload in audit_rows(app) if event.startswith("learned_")]
    # The row names the stage and the error type only: no document content.
    assert rows == [("learned_failed", {"job_id": "done", "stage": "reader", "error_type": "ReaderTimedOut",
                                        "actor": "local-operator"})]


def learned_sources(root):
    return sorted(item["source_id"] for path in (root / "learned" / "suppliers").glob("*.json")
                  for item in json.loads(path.read_text())["samples"])


def test_concurrent_learning_for_one_supplier_keeps_every_sample(tmp_path, monkeypatch):
    # One supplier's file is read, changed and written whole: without one writer at a time the last writer wins.
    monkeypatch.setenv("INV_STUDIO_LEARN", "1")
    app = create_app(tmp_path / "data", store_type=Store)
    invoice = verified(SCAN["text"], SCAN["boxes"])
    jobs = [f"job-{n}" for n in range(4)]
    for jid in jobs:
        app.state.store.job(jid, {"id": jid, "revision": 2, "status": "exported", "text": SCAN["text"],
                                  "boxes": SCAN["boxes"], "tables": [], "invoice": invoice})
    statuses = []
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        threads = [threading.Thread(target=lambda jid=jid: statuses.append(
            c.post(f"/api/learned/jobs/{jid}", headers=HEADERS).status_code)) for jid in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert statuses == [200] * 4
    assert learned_sources(tmp_path / "data") == jobs
    assert [event for event, _ in audit_rows(app) if event.startswith("learned_")] == ["learned_recorded"] * 4
    # The same in this process: each write has its own temporary file, and none is left behind.
    store = LearnedStore(tmp_path / "here")
    threads = [threading.Thread(target=store.record_verified, args=(invoice, SCAN["text"], jid)) for jid in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert learned_sources(tmp_path / "here") == jobs
    assert not [path for path in (tmp_path / "here").rglob("*.part*")]


def stalled_reader(tmp_path, seconds):
    """A reader whose learning child touches the returned marker once it has read the store, then waits."""
    marker = tmp_path / "child-read-the-store"
    stall = ("import pathlib, time; import app.learned as learned; learn = learned.LearnedStore._learn; "
             f"learned.LearnedStore._learn = lambda *a, **k: (pathlib.Path('{marker}').touch(), time.sleep({seconds}), "
             "learn(*a, **k))[2]")
    reader = tmp_path / "slow-reader"
    reader.write_text(f"#!/bin/sh\nshift 2\nexec {sys.executable} -c \"{stall}; import runpy; "
                      f"runpy.run_module('app.ocr_worker', run_name='__main__', alter_sys=True)\" \"$@\"\n")
    reader.chmod(0o700)
    return reader, marker


def wait_for(path):
    deadline = time.monotonic() + 60
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), "the learning child never read the store"


def test_a_forget_during_learning_is_not_undone_when_the_learning_lands(tmp_path, monkeypatch):
    # A forget arriving while the child waits must not be overwritten by the child's files, which still hold the
    # forgotten invoice's sample.
    reader, marker = stalled_reader(tmp_path, 1.5)
    monkeypatch.setenv("INV_STUDIO_LEARN", "1")
    monkeypatch.delenv("INV_STUDIO_CLOUD", raising=False)
    app = create_app(tmp_path / "data", store_type=Store)
    invoice = verified(SCAN["text"], SCAN["boxes"])
    app.state.learned.record_verified(invoice, SCAN["text"], "deleted")
    app.state.store.job("kept", {"id": "kept", "revision": 2, "status": "exported", "text": SCAN["text"],
                                 "boxes": SCAN["boxes"], "tables": [], "invoice": invoice})
    monkeypatch.setattr(engines.sys, "executable", str(reader))
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        statuses = []
        learning = threading.Thread(target=lambda: statuses.append(
            c.post("/api/learned/jobs/kept", headers=HEADERS).status_code))
        learning.start()
        wait_for(marker)
        app.state.learned.forget_sources(["deleted"])  # what deleting the invoice does
        learning.join()
    assert statuses == [200] and learned_sources(tmp_path / "data") == ["kept"]


def test_a_decision_waiting_for_a_learning_run_holds_no_database_write(tmp_path, monkeypatch):
    # Approving a correction waits for the learned store's lock, which a learning run holds for its whole child:
    # it must not wait inside a database transaction, or every other write fails behind it.
    reader, marker = stalled_reader(tmp_path, 4)
    monkeypatch.setenv("INV_STUDIO_LEARN", "1")
    monkeypatch.delenv("INV_STUDIO_CLOUD", raising=False)
    app = create_app(tmp_path / "data", store_type=Store)
    app.state.store.job("done", {"id": "done", "revision": 2, "status": "exported", "text": SCAN["text"],
                                 "boxes": SCAN["boxes"], "tables": [], "invoice": verified(SCAN["text"], SCAN["boxes"])})
    monkeypatch.setattr(engines.sys, "executable", str(reader))
    statuses = []
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        fid = c.post("/api/fine-rules/feedback", headers=HEADERS, json={
            "invoice_line": "synthetic invoice line", "correction": "reviewed correction",
            "evidence": "synthetic evidence"}).json()["Feedback ID"]
        learning = threading.Thread(target=lambda: statuses.append(
            c.post("/api/learned/jobs/done", headers=HEADERS).status_code))
        learning.start()
        wait_for(marker)
        deciding = threading.Thread(target=lambda: statuses.append(c.post(
            f"/api/fine-rules/feedback/{fid}/decision", headers=HEADERS, json={"decision": "Approved"}).status_code))
        deciding.start()
        time.sleep(0.5)  # the decision is now waiting for the learning run
        started = time.monotonic()
        app.state.store.audit("probe", {})
        assert time.monotonic() - started < 2 and deciding.is_alive()
        learning.join()
        deciding.join()
    assert statuses == [200, 200]
    assert app.state.learned.summary()["corrections"] == 1
