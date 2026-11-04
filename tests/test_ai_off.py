"""Security pack N9: with the admin's AI setting off, the server refuses every AI read; it never rewrites the options."""
import pytest

from tests.conftest import bearer
from tests.test_batch import OPTIONS, FakeReader, finished, plan, studio, upload

AI_OPTIONS = [{**OPTIONS, "ai_fallback": True}, {**OPTIONS, "ai_cross_check": True}, {**OPTIONS, "engine": "ai"}]


def set_ai(client, token, on):
    body = {"provider": "openai", "model": "", "ai_fallback": on}
    assert client.post("/api/settings", json=body, headers=bearer(token)).status_code == 200


def refused(response):
    assert response.status_code == 403, response.text
    assert "administrator has turned AI off" in response.json()["detail"]


@pytest.mark.parametrize("options", AI_OPTIONS)
def test_preflight_refuses_an_ai_read_when_ai_is_off(tmp_path, monkeypatch, options):
    app, client, token = studio(tmp_path, monkeypatch, FakeReader())
    set_ai(client, token, False)
    refused(client.post("/api/preflight", json={"options": options, "files": [{"name": "a.txt", "size": 1}]},
                        headers=bearer(token)))


@pytest.mark.parametrize("options", AI_OPTIONS)
def test_an_upload_planned_before_ai_was_turned_off_is_refused(tmp_path, monkeypatch, options):
    reader = FakeReader()
    app, client, token = studio(tmp_path, monkeypatch, reader)
    plan_token = plan(client, token, ["a.txt"], options)
    set_ai(client, token, False)
    refused(upload(client, token, plan_token, "a.txt", options))
    assert reader.calls == [] and app.state.store.jobs() == []


@pytest.mark.parametrize("options", AI_OPTIONS)
def test_a_retry_with_ai_is_refused_when_ai_is_off(tmp_path, monkeypatch, options):
    app, client, token = studio(tmp_path, monkeypatch, FakeReader())
    job = upload(client, token, plan(client, token, ["a.txt"]), "a.txt").json()
    assert finished(app, [job["id"]])
    revision = app.state.store.job(job["id"])["revision"]
    retry_token = plan(client, token, ["a.txt"], options)
    set_ai(client, token, False)
    refused(client.post(f"/api/jobs/{job['id']}/retry", json={"options": options, "preflight_token": retry_token},
                        headers=bearer(token)))
    assert app.state.store.job(job["id"])["revision"] == revision


def test_local_reads_still_run_when_ai_is_off(tmp_path, monkeypatch):
    app, client, token = studio(tmp_path, monkeypatch, FakeReader())
    set_ai(client, token, False)
    response = upload(client, token, plan(client, token, ["a.txt"]), "a.txt")
    assert response.status_code == 200, response.text
    assert response.json()["options"]["ai_fallback"] is False


@pytest.mark.parametrize("options", AI_OPTIONS)
def test_ai_reads_are_allowed_when_ai_is_on(tmp_path, monkeypatch, options):
    app, client, token = studio(tmp_path, monkeypatch, FakeReader())
    set_ai(client, token, True)
    job = upload(client, token, plan(client, token, ["a.txt"], options), "a.txt", options)
    assert job.status_code == 200, job.text
    assert options.items() <= job.json()["options"].items()
    assert finished(app, [job.json()["id"]])
    retry = client.post(f"/api/jobs/{job.json()['id']}/retry",
                        json={"options": options, "preflight_token": plan(client, token, ["a.txt"], options)},
                        headers=bearer(token))
    assert retry.status_code == 200, retry.text
