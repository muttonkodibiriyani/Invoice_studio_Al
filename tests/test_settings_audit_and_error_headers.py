"""Security pack N3 (AI settings changes are audited) and N4 (an unhandled error keeps the security headers)."""
import json

from tests.conftest import HEADERS, audit_rows


def local_app(tmp_path, monkeypatch):
    monkeypatch.delenv("INV_STUDIO_CLOUD", raising=False)
    from app.main import create_app
    from app.store import Store
    return create_app(tmp_path / "d", store_type=Store)


def local_client(app):
    from fastapi.testclient import TestClient
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))


def events(app, name):
    return [json.loads(payload) for event, payload in audit_rows(app) if event == name]


def test_saving_ai_settings_audits_who_and_before_and_after(tmp_path, monkeypatch):
    app = local_app(tmp_path, monkeypatch)
    app.state.store.secret("openai", "sk-test-secret-0123456789")
    with local_client(app) as c:
        first = {"provider": "anthropic", "model": "claude-test", "ai_fallback": False}
        assert c.post("/api/settings", headers=HEADERS, json=first).status_code == 200
        second = {"provider": "openai", "model": "gpt-test", "ai_fallback": True}
        assert c.post("/api/settings", headers=HEADERS, json=second).status_code == 200
    changed = events(app, "settings_changed")
    assert [(e["before"], e["after"]) for e in changed] == [
        ({"provider": "openai", "model": "", "ai_fallback": True}, first), (first, second)]
    assert {e["actor"] for e in changed} == {"local-operator"}
    assert "sk-test-secret" not in json.dumps(changed)


def test_selecting_and_removing_a_chatgpt_account_is_audited_without_a_token(tmp_path, monkeypatch):
    app = local_app(tmp_path, monkeypatch)
    store, auth = app.state.store, app.state.auth
    store.set("chatgpt_accounts", [{"id": "acct-1", "connected": True, "client_id": "client-x"}])
    store.secret("chatgpt:acct-1", {"refresh_token": "rt-secret-0123456789", "client_id": "client-x"})
    monkeypatch.setattr(auth, "disconnect", lambda aid: "Disconnected")
    with local_client(app) as c:
        assert c.post("/api/chatgpt/select", headers=HEADERS, json={"id": "acct-1"}).status_code == 200
        assert c.delete("/api/chatgpt/acct-1", headers=HEADERS).status_code == 200
    selected, removed = events(app, "subscription_selected"), events(app, "subscription_disconnected")
    assert [(e["provider"], e["account_id"], e["actor"]) for e in selected] == [("chatgpt", "acct-1", "local-operator")]
    assert [(e["provider"], e["account_id"], e["remote_revoked"], e["actor"]) for e in removed] == [
        ("chatgpt", "acct-1", True, "local-operator")]
    assert "rt-secret" not in json.dumps(selected + removed)


def boom_route(app):
    @app.get("/boom")
    def boom():
        raise RuntimeError("secret-ish detail /srv/data/owner.xlsx")


def assert_secure_500(response, cloud):
    assert response.status_code == 500
    assert "secret-ish" not in response.text and "RuntimeError" not in response.text
    assert response.json()["detail"].startswith("Something went wrong")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert len(response.headers["X-Request-Id"]) >= 8
    assert response.headers["X-Request-Id"] in response.json()["detail"]
    assert ("Strict-Transport-Security" in response.headers) == cloud
    if cloud:
        assert response.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_an_unhandled_error_answers_500_with_the_security_headers_in_local_mode(tmp_path, monkeypatch):
    app = local_app(tmp_path, monkeypatch)
    boom_route(app)
    with local_client(app) as c:
        assert_secure_500(c.get("/boom"), cloud=False)


def test_an_unhandled_error_answers_500_with_the_security_headers_and_hsts_in_cloud_mode(app):
    from fastapi.testclient import TestClient
    boom_route(app)
    with TestClient(app) as c:
        assert_secure_500(c.get("/boom"), cloud=True)
