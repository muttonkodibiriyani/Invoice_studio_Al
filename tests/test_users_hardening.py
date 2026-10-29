"""The Google project's user pool is shared with Product Intelligence: the app changes only the sign-ins it created.
Plus the guards around bootstrap owners, failed invitations, link addresses and candidate revisions."""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store
from tests.conftest import HEADERS, OWNER, FakeIdentity, FakeUserAdmin, audit_rows, bearer


def create(client, owner, email, role="member", headers=None):
    response = client.post("/api/users", json={"email": email, "name": "", "role": role}, headers={**bearer(owner), **(headers or {})})
    assert response.status_code == 201, response.text
    return response.json()


def signed_in(client, provider, owner, email, role="member"):
    issued = create(client, owner, email, role=role)
    provider.set_password(issued["link"].split("#oobCode=")[1], "a long enough password")
    return issued["user"]["uid"], provider.sign_in(email, "a long enough password")


def test_sign_in_the_app_did_not_create_is_managed_in_the_workspace_only(client, provider, owner):
    # For example a Product Intelligence login that an operator added to the workspace directly.
    token = provider.seed("pi-uid", "pi@example.test")
    client.app.state.users.create("pi-uid", "pi@example.test", "", "member", invited_by="test", status="active", login_origin="existing")
    assert client.get("/api/session", headers=bearer(token)).status_code == 200
    calls = len(provider.calls)

    assert client.post("/api/users/pi-uid/disable", json={}, headers=bearer(owner)).json()["user"]["status"] == "disabled"
    refused = client.get("/api/session", headers=bearer(token))
    assert refused.status_code == 403 and "disabled" in refused.json()["detail"]
    assert provider.accounts["pi-uid"]["disabled"] is False and "pi-uid" not in provider.revoked

    assert client.post("/api/users/pi-uid/enable", json={}, headers=bearer(owner)).json()["user"]["status"] == "active"
    assert client.get("/api/session", headers=bearer(token)).status_code == 200

    link = client.post("/api/users/pi-uid/password-link", json={}, headers=bearer(owner))
    assert link.status_code == 409 and "did not create" in link.json()["detail"]

    assert client.delete("/api/users/pi-uid", headers=bearer(owner)).status_code == 200
    assert "pi-uid" in provider.accounts, "the other app's sign-in stays"
    assert client.get("/api/session", headers=bearer(token)).status_code == 403
    assert provider.calls[calls:] == [], "the identity provider was never asked to change this sign-in"
    events = [event for event, _ in audit_rows(client.app)]
    assert {"user.disabled", "user.enabled", "user.deleted"} <= set(events)
    assert "user.reset_link_issued" not in events


def test_bootstrap_owner_cannot_be_deleted_and_a_removed_owner_is_refused_cleanly(client, provider, owner):
    _, second = signed_in(client, provider, owner, "second@example.test", role="admin")
    response = client.delete("/api/users/owner-uid", headers=bearer(second))
    assert response.status_code == 409 and "service configuration" in response.json()["detail"]
    assert client.app.state.users.get("owner-uid") is not None
    # Removed anyway (break-glass SQL): the next request is a clear refusal, not a 500 and not a silent re-bootstrap.
    client.app.state.users.soft_delete("owner-uid")
    refused = client.get("/api/session", headers=bearer(owner))
    assert refused.status_code == 403 and "removed from this workspace" in refused.json()["detail"]
    assert client.app.state.users.get("owner-uid") is None


def test_a_failed_invitation_removes_the_sign_in_it_created(app, provider, owner):
    def broken(*args, **kwargs):
        raise RuntimeError("database unavailable")
    app.state.users.create = broken
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/users", json={"email": "half@example.test", "name": "", "role": "member"}, headers=bearer(owner))
    assert response.status_code == 500
    assert provider._uid_for("half@example.test") is None
    created = [call for call in provider.calls if call[0] == "create"]
    deleted = [call for call in provider.calls if call[0] == "delete"]
    assert len(created) == 1 and len(deleted) == 1
    assert not any("half@example.test" in payload for _, payload in audit_rows(app))


@pytest.fixture
def preview_client(tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_ALLOWED_ORIGINS", "https://studio.example.test,https://studio--preview-1a2b.example.test")
    provider = FakeUserAdmin()
    app = create_app(tmp_path / "data", identity=FakeIdentity(provider), store_type=Store)
    with TestClient(app) as client:
        owner = provider.seed("owner-uid", OWNER)
        assert client.get("/api/session", headers=bearer(owner)).status_code == 200
        yield client, owner


def test_links_point_at_the_workspace_address_the_administrator_is_using(preview_client):
    client, owner = preview_client
    preview = create(client, owner, "p@example.test", headers={"Origin": "https://studio--preview-1a2b.example.test"})
    assert preview["link"].startswith("https://studio--preview-1a2b.example.test/set-password#oobCode=")
    same_host = create(client, owner, "h@example.test", headers={"Origin": "http://testserver"})
    assert same_host["link"].startswith("http://testserver/set-password#oobCode=")
    default = create(client, owner, "d@example.test")
    assert default["link"].startswith("https://studio.example.test/set-password#oobCode=")
    foreign = client.post("/api/users", json={"email": "f@example.test", "name": "", "role": "member"},
                          headers={**bearer(owner), "Origin": "https://elsewhere.example.test"})
    assert foreign.status_code == 403 and client.app.state.users.by_email("f@example.test") is None


@pytest.mark.parametrize("recover, expected", [("0", "processing"), ("1", "error")])
def test_a_candidate_revision_leaves_the_live_instance_jobs_alone(recover, expected, tmp_path, monkeypatch):
    data = tmp_path / "data"
    store = create_app(data, identity=FakeIdentity(FakeUserAdmin()), store_type=Store).state.store
    store.job("running", {"id": "running", "revision": 1, "status": "processing"})
    monkeypatch.setenv("INV_STUDIO_RECOVER_JOBS", recover)
    restarted = create_app(data, identity=FakeIdentity(FakeUserAdmin()), store_type=Store)
    assert restarted.state.store.job("running")["status"] == expected


def test_forgot_password_never_touches_the_identity_provider(client, provider, owner):
    uid = create(client, owner, "quiet@example.test")["user"]["uid"]
    calls = len(provider.calls)
    for email in ("quiet@example.test", "pi-only@example.test", "nobody@example.test"):
        assert client.post("/api/password/forgot", json={"email": email}, headers=HEADERS).status_code == 200
    assert provider.calls[calls:] == []
    rows = [json.loads(payload) for event, payload in audit_rows(client.app) if event == "user.reset_requested"]
    assert [row["uid"] for row in rows] == [uid]
