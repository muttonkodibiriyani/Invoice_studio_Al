"""Large batches: admission separate from processing, reader slots, restart recovery and plan renewal.

The reader is a fake standing in for app.engines.process_job; the rest of the pipeline (store, rules, audit) is real.
"""
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

import pytest

from app import engines
from app import main as main_module
from app.main import create_app
from app.store import Store
from tests.conftest import OWNER, FakeIdentity, FakeUserAdmin, bearer

OPTIONS = {"engine": "auto", "prefer_native_text": True, "ai_fallback": False, "provider": "openai", "model": "", "language": "en"}


class BlobStore(Store):
    """The SQLite store with the cloud store's blob hooks as no-ops: uploads stay on local disk in the test."""

    def persist_blob(self, path):
        pass

    def ensure_blob(self, path):
        pass


class FakeReader:
    """Stands in for engines.process_job: records calls and peak concurrency; optionally waits for a gate."""

    def __init__(self, gate=None):
        self.gate = gate
        self.calls = []
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def __call__(self, path, options, store, ai_reader, progress, **context):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(path.name)
        try:
            progress("fake", "Reading document")
            if self.gate is not None:
                assert self.gate.wait(timeout=60), "the test never opened the gate"
            return {"invoice": {"number": "INV-" + path.stem[:8], "lines": []}, "text": "INVOICE " + path.stem, "boxes": [],
                    "trace": [{"engine": "fake", "status": "extracted"}], "selected_engine": "fake", "completeness": 40,
                    "document_type_hint": None, "extraction_note": None, "evidence": {}, "readers": {},
                    "annotated_evidence": {}}
        finally:
            with self.lock:
                self.active -= 1


def studio(tmp_path, monkeypatch, reader, **env):
    """An app on tmp_path/data with the fake reader installed and the bootstrap administrator signed in."""
    for name in ("INV_STUDIO_MAX_QUEUED", "INV_STUDIO_WORKERS", "INV_STUDIO_RECOVER_JOBS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(main_module, "process_job", reader)
    provider = FakeUserAdmin()
    app = create_app(tmp_path / "data", identity=FakeIdentity(provider), store_type=BlobStore)
    from fastapi.testclient import TestClient
    client = TestClient(app)
    token = provider.seed("owner-uid", OWNER)
    assert client.get("/api/session", headers=bearer(token)).status_code == 200
    return app, client, token


def plan(client, token, names, options=OPTIONS):
    files = [{"name": name, "size": len(content(name))} for name in names]
    response = client.post("/api/preflight", json={"options": options, "files": files}, headers=bearer(token))
    assert response.status_code == 200, response.text
    plan_token = response.json()["token"]
    assert client.post("/api/preflight/confirm", json={"token": plan_token}, headers=bearer(token)).status_code == 200
    return plan_token


def content(name):
    return f"INVOICE {name}\nTotal 10.00\n".encode()


def upload(client, token, plan_token, name, options=OPTIONS):
    return client.post("/api/invoices", files={"file": (name, content(name), "text/plain")},
                       data={"options": json.dumps(options), "preflight_token": plan_token}, headers=bearer(token))


def statuses(app, ids):
    return {jid: app.state.store.job(jid)["status"] for jid in ids}


def wait_until(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def finished(app, ids):
    return wait_until(lambda: all(status not in ("queued", "processing") for status in statuses(app, ids).values()))


def test_a_batch_of_120_invoices_is_admitted_whole_and_every_one_completes(tmp_path, monkeypatch):
    reader = FakeReader()
    app, client, token = studio(tmp_path, monkeypatch, reader)
    names = [f"invoice-{n:03d}.txt" for n in range(120)]
    plan_token = plan(client, token, names)  # the plan takes the whole batch, not 100 files
    responses = [upload(client, token, plan_token, name) for name in names]
    assert [r.status_code for r in responses] == [200] * 120, [r.text for r in responses if r.status_code != 200]
    ids = [r.json()["id"] for r in responses]
    assert finished(app, ids)
    assert set(statuses(app, ids).values()) == {"review"}
    assert sorted(reader.calls) == sorted(f"{jid}.txt" for jid in ids) and len(reader.calls) == 120
    assert reader.peak <= app.state.pool._max_workers
    queue = client.get("/api/queue", headers=bearer(token)).json()
    assert queue["queued"] == [] and queue["processing"] == [] and queue["limit"] == 500
    jobs = client.get("/api/state", headers=bearer(token)).json()["jobs"]
    assert len(jobs) == 120 and all(job["status"] == "review" for job in jobs)


def test_queued_jobs_wait_their_turn_and_only_the_admission_limit_refuses(tmp_path, monkeypatch):
    gate = threading.Event()
    reader = FakeReader(gate)
    app, client, token = studio(tmp_path, monkeypatch, reader, INV_STUDIO_MAX_QUEUED="3", INV_STUDIO_WORKERS="1")
    names = [f"inv-{n}.txt" for n in range(4)]
    plan_token = plan(client, token, names)
    admitted = [upload(client, token, plan_token, name) for name in names[:3]]
    assert [r.status_code for r in admitted] == [200, 200, 200]
    ids = [r.json()["id"] for r in admitted]
    assert wait_until(lambda: statuses(app, ids)[ids[0]] == "processing")
    assert statuses(app, ids[1:]) == {ids[1]: "queued", ids[2]: "queued"}  # beyond the single worker: waiting, not refused
    queue = client.get("/api/queue", headers=bearer(token)).json()
    assert queue["processing"] == [ids[0]] and sorted(queue["queued"]) == sorted(ids[1:]) and queue["limit"] == 3
    refused = upload(client, token, plan_token, names[3])
    assert refused.status_code == 429 and refused.json()["detail"].startswith("Processing queue is full (3 invoices")
    gate.set()
    assert finished(app, ids)
    assert upload(client, token, plan_token, names[3]).status_code == 200  # the refused file is still in the plan


def test_workers_follow_the_reader_slots_unless_set(tmp_path, monkeypatch):
    app, _, _ = studio(tmp_path, monkeypatch, FakeReader())
    assert app.state.pool._max_workers == engines.AI_CONCURRENCY + engines.LOCAL_OCR_CONCURRENCY == 5
    app, _, _ = studio(tmp_path / "two", monkeypatch, FakeReader(), INV_STUDIO_WORKERS="2")
    assert app.state.pool._max_workers == 2


@pytest.mark.parametrize("value, expected", [("", 4), ("abc", 4), ("0", 1), ("3", 3), ("99", 16)])
def test_reader_slot_counts_come_from_the_environment_within_bounds(value, expected, monkeypatch):
    monkeypatch.setenv("INV_AI_CONCURRENCY", value)
    assert engines.concurrency_from_env("INV_AI_CONCURRENCY", 4, 16) == expected


def gone_quiet(monkeypatch):
    """From here on, every heartbeat written so far is older than LEASE_STALE: the stopped process's jobs can be taken."""
    monkeypatch.setattr(main_module, "lease_clock", lambda: time.time() + main_module.LEASE_STALE + 1)


def test_a_restart_queues_the_unfinished_jobs_again_in_order(tmp_path, monkeypatch):
    gate = threading.Event()
    first = FakeReader(gate)
    app, client, token = studio(tmp_path, monkeypatch, first, INV_STUDIO_WORKERS="1")
    names = [f"inv-{n}.txt" for n in range(3)]
    plan_token = plan(client, token, names)
    ids = [upload(client, token, plan_token, name).json()["id"] for name in names]
    assert wait_until(lambda: statuses(app, ids)[ids[0]] == "processing")
    assert wait_until(lambda: first.active == 1)  # inside the gated read: the next studio() cannot swap it out
    assert statuses(app, ids[1:]) == {ids[1]: "queued", ids[2]: "queued"}
    # The process dies: its running job stays "processing" and the waiting ones "queued" in the shared database.
    app.state.pool.shutdown(wait=False, cancel_futures=True)
    gone_quiet(monkeypatch)
    try:
        second = FakeReader()
        # One worker, so the order the reader sees is the order recovery queued the jobs in. With several, the
        # threads race to the reader and the read order is the scheduler's, not the app's.
        restarted, client2, token2 = studio(tmp_path, monkeypatch, second, INV_STUDIO_WORKERS="1")
        assert finished(restarted, ids)
        assert set(statuses(restarted, ids).values()) == {"review"}
        assert second.calls == [f"{jid}.txt" for jid in ids]  # oldest first
        with restarted.state.store.connection() as c:
            events = [(e, json.loads(p)) for e, p in c.execute("SELECT event,payload FROM audit WHERE event='jobs_requeued_after_restart'")]
        assert len(events) == 1 and events[0][1]["job_ids"] == ids and events[0][1]["count"] == 3
        assert client2.get("/api/queue", headers=bearer(token2)).json()["queued"] == []
    finally:
        gate.set()


class InlinePool(ThreadPoolExecutor):
    """A pool whose worker picks a job up the instant it is submitted: the fastest interleaving a real pool allows."""

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        future.set_result(fn(*args, **kwargs))
        return future


def test_recovered_jobs_start_only_once_the_app_is_fully_built(tmp_path, monkeypatch):
    # A recovered job that a worker reads at once must find every helper of create_app defined (it once hit a
    # NameError on a closure defined after the recovery block and ended in "error").
    gate = threading.Event()
    first = FakeReader(gate)
    app, client, token = studio(tmp_path, monkeypatch, first, INV_STUDIO_WORKERS="1")
    ids = [upload(client, token, plan(client, token, ["inv-0.txt"]), "inv-0.txt").json()["id"]]
    assert wait_until(lambda: statuses(app, ids)[ids[0]] == "processing")
    assert wait_until(lambda: first.active == 1)  # inside the gated read: the next studio() cannot swap it out
    app.state.pool.shutdown(wait=False, cancel_futures=True)
    gone_quiet(monkeypatch)
    try:
        monkeypatch.setattr(main_module, "ThreadPoolExecutor", InlinePool)
        restarted, _, _ = studio(tmp_path, monkeypatch, FakeReader())
        job = restarted.state.store.job(ids[0])
        assert (job["status"], job.get("error")) == ("review", None)
    finally:
        gate.set()


def test_a_candidate_with_recovery_off_leaves_the_live_jobs_alone(tmp_path, monkeypatch):
    app, _, _ = studio(tmp_path, monkeypatch, FakeReader())
    store = app.state.store
    store.job("running", {"id": "running", "revision": 1, "status": "processing", "options": OPTIONS, "path": "x", "created_at": "2026-10-27T10:00:00+00:00"})
    store.job("waiting", {"id": "waiting", "revision": 1, "status": "queued", "options": OPTIONS, "path": "x", "created_at": "2026-10-27T10:00:01+00:00"})
    candidate, client, token = studio(tmp_path, monkeypatch, FakeReader(), INV_STUDIO_RECOVER_JOBS="0")
    assert statuses(candidate, ["running", "waiting"]) == {"running": "processing", "waiting": "queued"}
    assert client.get("/api/queue", headers=bearer(token)).json() | {"queued": [], "processing": []} == client.get("/api/queue", headers=bearer(token)).json()


def test_a_job_that_cannot_be_queued_again_says_so(tmp_path, monkeypatch):
    app, _, _ = studio(tmp_path, monkeypatch, FakeReader())
    app.state.store.job("orphan", {"id": "orphan", "revision": 1, "status": "queued"})  # no file, no options
    restarted, _, _ = studio(tmp_path, monkeypatch, FakeReader())
    job = restarted.state.store.job("orphan")
    assert job["status"] == "error" and "could not be queued again" in job["error"]


def test_a_confirmed_plan_lives_on_while_uploads_arrive_and_when_renewed(tmp_path, monkeypatch):
    app, client, token = studio(tmp_path, monkeypatch, FakeReader())
    names = [f"inv-{n}.txt" for n in range(3)]
    monkeypatch.setattr(main_module, "PLAN_TTL", 1)
    plan_token = plan(client, token, names)
    monkeypatch.setattr(main_module, "PLAN_TTL", 600)
    assert upload(client, token, plan_token, names[0]).status_code == 200  # each upload extends the plan
    time.sleep(1.2)
    assert upload(client, token, plan_token, names[1]).status_code == 200
    monkeypatch.setattr(main_module, "PLAN_TTL", 1)
    short = plan(client, token, ["late.txt"])
    monkeypatch.setattr(main_module, "PLAN_TTL", 600)
    renewed = client.post("/api/preflight/renew", json={"token": short}, headers=bearer(token))
    assert renewed.status_code == 200 and renewed.json() == {"renewed": True, "expires_in": 600, "files_remaining": 1}
    time.sleep(1.2)
    assert upload(client, token, short, "late.txt").status_code == 200
    live = plan(client, token, ["live.txt"])
    assert client.post("/api/preflight/renew", json={"token": "no-such-plan"}, headers=bearer(token)).status_code == 409
    unconfirmed = client.post("/api/preflight", json={"options": OPTIONS, "files": [{"name": "u.txt", "size": 1}]}, headers=bearer(token)).json()["token"]
    assert client.post("/api/preflight/renew", json={"token": unconfirmed}, headers=bearer(token)).status_code == 409
    assert client.post("/api/preflight/renew", json={"token": live}, headers=bearer(token)).status_code == 200
