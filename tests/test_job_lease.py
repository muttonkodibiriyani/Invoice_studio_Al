"""Two processes on one database (overlapping revisions): a job is taken over only once its holder has gone quiet, and
the old holder can no longer change it (security review M5)."""
import json
import threading

from app import main as main_module
from tests.conftest import bearer
from tests.test_batch import OPTIONS, FakeReader, gone_quiet, plan, statuses, studio, upload, wait_until


def dropped(app):
    with app.state.store.connection() as c:
        return [json.loads(p)["job_id"] for (p,) in c.execute("SELECT payload FROM audit WHERE event='stale_result_dropped'")]


def test_an_old_process_that_resumes_cannot_change_the_jobs_a_new_one_took_over(tmp_path, monkeypatch):
    gate = threading.Event()
    reader = FakeReader(gate)
    old, client, token = studio(tmp_path, monkeypatch, reader, INV_STUDIO_WORKERS="1")
    names = ["inv-0.txt", "inv-1.txt"]
    plan_token = plan(client, token, names)
    ids = [upload(client, token, plan_token, name).json()["id"] for name in names]
    assert wait_until(lambda: statuses(old, ids) == {ids[0]: "processing", ids[1]: "queued"})
    assert wait_until(lambda: reader.active == 1)  # inside the gated read: the next studio() cannot swap it out
    try:
        gone_quiet(monkeypatch)  # the old process is alive but stalled: its heartbeat has stopped
        new, _, _ = studio(tmp_path, monkeypatch, FakeReader())
        assert wait_until(lambda: set(statuses(new, ids).values()) == {"review"})
        after = {jid: new.state.store.job(jid) for jid in ids}
    finally:
        gate.set()
    # The old worker finishes its read, then picks up the job still waiting in its own queue.
    assert wait_until(lambda: sorted(dropped(old)) == sorted(ids))
    old.state.pool.shutdown(wait=True)
    for jid in ids:
        job = new.state.store.job(jid)
        assert (job["status"], job["revision"], job["invoice"], job["holder"]) == (
            "review", after[jid]["revision"], after[jid]["invoice"], after[jid]["holder"])


def test_a_live_holder_keeps_its_jobs_and_a_quiet_one_loses_them(tmp_path, monkeypatch):
    gate = threading.Event()
    reader = FakeReader(gate)
    live, client, token = studio(tmp_path, monkeypatch, reader, INV_STUDIO_WORKERS="1")
    jid = upload(client, token, plan(client, token, ["inv-0.txt"]), "inv-0.txt").json()["id"]
    try:
        assert wait_until(lambda: statuses(live, [jid])[jid] == "processing")
        assert wait_until(lambda: reader.active == 1)  # inside the gated read: the next studio() cannot swap it out
        holder = live.state.store.job(jid)["holder"]
        other, _, _ = studio(tmp_path, monkeypatch, FakeReader())
        assert other.state.store.job(jid)["holder"] == holder  # a fresh heartbeat: left alone
        clock = main_module.lease_clock() + main_module.LEASE_STALE + 1
        monkeypatch.setattr(main_module, "lease_clock", lambda: clock)
        live.state.beat()  # the holder beats: still not stale for the other process
        other.state.beat()
        assert statuses(other, [jid])[jid] == "processing"
        assert live.state.store.job(jid)["heartbeat"] == clock
        monkeypatch.setattr(main_module, "lease_clock", lambda: clock + main_module.LEASE_STALE + 1)
        other.state.beat()  # the holder has stopped beating
        assert wait_until(lambda: statuses(other, [jid])[jid] == "review")
        assert other.state.store.job(jid)["holder"] != holder
    finally:
        gate.set()
    assert wait_until(lambda: dropped(live) == [jid])


def test_a_candidate_with_recovery_off_never_takes_over_even_a_quiet_holder(tmp_path, monkeypatch):
    gate = threading.Event()
    reader = FakeReader(gate)
    live, client, token = studio(tmp_path, monkeypatch, reader, INV_STUDIO_WORKERS="1")
    jid = upload(client, token, plan(client, token, ["inv-0.txt"]), "inv-0.txt").json()["id"]
    try:
        assert wait_until(lambda: statuses(live, [jid])[jid] == "processing")
        assert wait_until(lambda: reader.active == 1)  # inside the gated read: the next studio() cannot swap it out
        gone_quiet(monkeypatch)
        candidate, client2, token2 = studio(tmp_path, monkeypatch, FakeReader(), INV_STUDIO_RECOVER_JOBS="0")
        candidate.state.beat()
        assert statuses(candidate, [jid])[jid] == "processing"
        assert client2.get("/api/queue", headers=bearer(token2)).json()["processing"] == []
    finally:
        gate.set()


def test_unfinished_jobs_are_read_through_an_index(tmp_path, monkeypatch):
    from app.store import UNFINISHED
    app, _, _ = studio(tmp_path, monkeypatch, FakeReader())
    with app.state.store.connection() as c:
        plan_rows = c.execute(f"EXPLAIN QUERY PLAN SELECT payload FROM jobs WHERE {UNFINISHED}").fetchall()
    assert "jobs_unfinished" in " ".join(str(row[-1]) for row in plan_rows)


def test_the_server_beats_while_it_runs_and_stops_when_it_stops(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    beats = threading.Semaphore(0)
    monkeypatch.setattr(main_module, "LEASE_BEAT", 0.01)
    app, _, _ = studio(tmp_path, monkeypatch, FakeReader())
    monkeypatch.setattr(app.state, "beat", beats.release)
    with TestClient(app):
        assert beats.acquire(timeout=10)
    assert wait_until(lambda: "job-heartbeat" not in {t.name for t in threading.enumerate()})


def test_a_job_is_taken_over_up_to_the_cap_and_then_marked_as_an_error(tmp_path, monkeypatch):
    """Each instance's read never finishes while the test runs, so it stops beating as a held process would."""
    gate = threading.Event()
    now = [main_module.lease_clock()]
    monkeypatch.setattr(main_module, "lease_clock", lambda: now[0])
    reader = FakeReader(gate)
    first, client, token = studio(tmp_path, monkeypatch, reader, INV_STUDIO_WORKERS="1")
    jid = upload(client, token, plan(client, token, ["inv-0.txt"]), "inv-0.txt").json()["id"]
    apps = [first]
    try:
        assert wait_until(lambda: statuses(first, [jid])[jid] == "processing")
        assert wait_until(lambda: reader.active == 1)  # inside the gated read: the next studio() cannot swap it out
        for taken in range(1, main_module.TAKEOVER_CAP + 1):
            now[0] += main_module.LEASE_STALE + 1  # the holder is held and has stopped beating
            reader = FakeReader(gate)
            apps.append(studio(tmp_path, monkeypatch, reader, INV_STUDIO_WORKERS="1")[0])  # the next start takes it
            assert wait_until(lambda: reader.calls == [f"{jid}.txt"])  # below the cap: queued and read again
            job = apps[-1].state.store.job(jid)
            assert (job["status"], job["takeovers"]) == ("processing", taken)
        now[0] += main_module.LEASE_STALE + 1
        reader = FakeReader(gate)
        last, client2, token2 = studio(tmp_path, monkeypatch, reader, INV_STUDIO_WORKERS="1")
        apps.append(last)
        job = last.state.store.job(jid)
        assert job["status"] == "error" and job["error"].startswith("Processing this invoice was interrupted")
        assert job["takeovers"] == main_module.TAKEOVER_CAP
        assert reader.calls == [] and client2.get("/api/queue", headers=bearer(token2)).json()["queued"] == []
        last.state.beat()  # a finished job is not looked at again
        with last.state.store.connection() as c:
            rows = [json.loads(p) for (p,) in c.execute("SELECT payload FROM audit WHERE event='job_takeover_cap_reached'")]
        assert [{k: v for k, v in row.items() if k != "actor"} for row in rows] == [{"job_id": jid}]  # the job id only
    finally:
        gate.set()
    # Every held instance finishes its read late; none of them may change the job the last one closed.
    assert wait_until(lambda: dropped(last) == [jid] * (len(apps) - 1))  # one shared database: every late result
    for app in apps:
        app.state.pool.shutdown(wait=True)
    assert last.state.store.job(jid)["status"] == "error"


def test_a_retry_after_the_cap_starts_the_count_again(tmp_path, monkeypatch):
    app, client, token = studio(tmp_path, monkeypatch, FakeReader(), INV_STUDIO_WORKERS="1")
    jid = upload(client, token, plan(client, token, ["inv-0.txt"]), "inv-0.txt").json()["id"]
    assert wait_until(lambda: statuses(app, [jid])[jid] == "review")
    with app.state.store.connection(True) as c:
        job = app.state.store.job(jid)
        job.update(status="error", takeovers=main_module.TAKEOVER_CAP)
        app.state.store.job(jid, job, c)
    response = client.post(f"/api/jobs/{jid}/retry", json={"options": OPTIONS, "preflight_token": plan(client, token, ["inv-0.txt"])},
                           headers=bearer(token))
    assert response.status_code == 200, response.text
    assert wait_until(lambda: statuses(app, [jid])[jid] == "review")
    assert app.state.store.job(jid)["takeovers"] == 0
