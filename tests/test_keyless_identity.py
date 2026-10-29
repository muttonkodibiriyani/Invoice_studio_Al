"""INV_STUDIO_IDENTITY_ADMIN=0: a cloud deployment (Azure) that holds no Google credential for sign-in.

It verifies ID tokens with Google's public certificates, checks revocation and disabled accounts through Identity
Toolkit accounts:lookup with the public web API key, refuses on any lookup failure, and leaves user management to the
primary workspace. Nothing here calls Google: the lookup is replaced.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.store import Store
from tests.conftest import HEADERS

CLAIMS = {"uid": "owner-uid", "email": "owner@example.test", "email_verified": True, "iat": 1000, "firebase": {}}
PEOPLE = {"member": {"uid": "member-uid", "email": "member@example.test"}}


@pytest.fixture
def no_google_credential(monkeypatch, tmp_path):
    for name in ("GOOGLE_APPLICATION_CREDENTIALS", "FIREBASE_AUTH_EMULATOR_HOST", "INV_STUDIO_TEST_AUTH_EMULATOR",
                 "INV_STUDIO_AUTH_TENANT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_GCE_CHECK", "True")  # no metadata server either
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "gcloud"))  # no gcloud application-default login
    monkeypatch.setenv("INV_STUDIO_CLOUD", "1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-keyless")
    monkeypatch.setenv("INV_STUDIO_FIREBASE_API_KEY", "web-key")
    monkeypatch.setenv("INV_STUDIO_PUBLIC_URL", "https://studio.example.test")


@pytest.fixture
def keyless(no_google_credential, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_IDENTITY_ADMIN", "0")
    from app.authentication import CloudIdentity
    identity = CloudIdentity()
    identity.client.verify_id_token = lambda token, check_revoked: (
        pytest.fail("revocation needs an Admin credential") if check_revoked else {**CLAIMS, **PEOPLE.get(token, {})})
    return identity


def lookup(monkeypatch, answer):
    sent = []
    def post(url, **kwargs):
        sent.append((url, kwargs))
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    return sent


def test_it_starts_without_a_google_credential(keyless):
    assert keyless.admin is None


def test_with_the_admin_setting_on_a_google_credential_is_still_required(no_google_credential, monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError
    from app.authentication import CloudIdentity
    monkeypatch.delenv("INV_STUDIO_IDENTITY_ADMIN", raising=False)
    with pytest.raises(DefaultCredentialsError):
        CloudIdentity()


@pytest.mark.parametrize("platform", [{}, {"K_SERVICE": "inv-studio-api"}, {"CONTAINER_APP_NAME": "inv-studio"}])
def test_the_auth_emulator_is_refused_on_any_host_without_the_test_flag(no_google_credential, monkeypatch, platform):
    from app.authentication import CloudIdentity
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    for name, value in platform.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match="for local tests only"):
        CloudIdentity()


def test_the_test_suite_flag_lets_the_auth_emulator_through(no_google_credential, monkeypatch):
    from app.authentication import CloudIdentity
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    monkeypatch.setenv("INV_STUDIO_TEST_AUTH_EMULATOR", "1")
    monkeypatch.setenv("INV_STUDIO_IDENTITY_ADMIN", "0")
    assert CloudIdentity().admin is None


@pytest.mark.parametrize("value", ["", "false", "no", " 0", "2"])
def test_the_setting_is_0_or_1_and_nothing_else(no_google_credential, monkeypatch, value):
    from app.authentication import CloudIdentity
    monkeypatch.setenv("INV_STUDIO_IDENTITY_ADMIN", value)
    with pytest.raises(ValueError, match="INV_STUDIO_IDENTITY_ADMIN must be 0 or 1"):
        CloudIdentity()


def test_keyless_needs_the_web_api_key(no_google_credential, monkeypatch):
    from app.authentication import CloudIdentity
    monkeypatch.setenv("INV_STUDIO_IDENTITY_ADMIN", "0")
    monkeypatch.setenv("INV_STUDIO_FIREBASE_API_KEY", "")
    with pytest.raises(ValueError, match="INV_STUDIO_FIREBASE_API_KEY"):
        CloudIdentity()


def test_a_sensitive_call_looks_up_the_account_with_the_callers_own_token(keyless, monkeypatch):
    sent = lookup(monkeypatch, (200, {"users": [{"localId": "owner-uid", "validSince": "1000"}]}))
    assert keyless.verify("Bearer the-token", True)["uid"] == "owner-uid"
    url, kwargs = sent[0]
    assert url == "https://identitytoolkit.googleapis.com/v1/accounts:lookup"
    assert kwargs["json"] == {"idToken": "the-token"}
    assert kwargs["headers"] == {"X-Goog-Api-Key": "web-key", "Referer": "https://studio.example.test/"}
    assert kwargs["timeout"] <= 5


def test_a_read_does_not_look_up(keyless, monkeypatch):
    sent = lookup(monkeypatch, AssertionError("not called"))
    assert keyless.verify("Bearer the-token")["uid"] == "owner-uid"
    assert sent == []


@pytest.mark.parametrize("answer", [
    (200, {"users": [{"localId": "owner-uid", "validSince": "1001"}]}),  # revoked after the token was issued
    (200, {"users": [{"localId": "owner-uid", "validSince": "1000", "disabled": True}]}),
    (200, {"users": [{"localId": "someone-else", "validSince": "0"}]}),
    (200, {}),
    (400, {"error": {"message": "USER_NOT_FOUND"}}),
    (503, {}),
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("no route"),
], ids=["revoked", "disabled", "other-user", "no-user", "google-error", "unavailable", "timeout", "network"])
def test_a_sensitive_call_is_refused_unless_the_lookup_clears_the_account(keyless, monkeypatch, answer):
    lookup(monkeypatch, answer)
    with pytest.raises(ValueError, match="could not be verified"):
        keyless.verify("Bearer the-token", True)


@pytest.fixture
def workspace(keyless, monkeypatch, tmp_path):
    """The keyless app with its bootstrap owner signed in, an existing member and a member whose sign-in the app
    created (as after restoring a database that used identity admin)."""
    from app.main import create_app

    def post(url, json, **kwargs):
        uid = {**CLAIMS, **PEOPLE.get(json["idToken"], {})}["uid"]
        return httpx.Response(200, json={"users": [{"localId": uid, "validSince": "0"}]}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    keyless.bootstrap_admins = {"owner@example.test"}
    app = create_app(tmp_path / "d", identity=keyless, store_type=Store)
    with TestClient(app) as client:
        as_owner = {"Authorization": "Bearer owner", **HEADERS}
        assert client.get("/api/session", headers=as_owner).status_code == 200  # bootstraps the owner
        app.state.users.create("member-uid", "member@example.test", "Member", role="member", invited_by="owner@example.test",
                               status="active", login_origin="existing")
        app.state.users.create("app-uid", "app@example.test", "App login", role="member", invited_by="owner@example.test",
                               status="active", login_origin="app")
        yield client, as_owner


def test_an_administrator_still_lists_and_disables_users_and_it_takes_effect_on_the_next_read(workspace):
    client, as_owner = workspace
    as_member = {"Authorization": "Bearer member", **HEADERS}
    assert client.get("/api/session", headers=as_member).status_code == 200
    assert {u["uid"] for u in client.get("/api/users", headers=as_owner).json()["users"]} >= {"owner-uid", "member-uid"}
    assert client.post("/api/users/member-uid/disable", json={}, headers=as_owner).json()["user"]["status"] == "disabled"
    assert client.get("/api/session", headers=as_member).status_code == 403
    assert client.post("/api/users/member-uid/enable", json={}, headers=as_owner).status_code == 200
    assert client.get("/api/session", headers=as_member).status_code == 200


def test_an_administrator_still_changes_roles_and_deletes_users(workspace):
    client, as_owner = workspace
    assert client.post("/api/users/member-uid/role", json={"role": "admin"}, headers=as_owner).json()["user"]["role"] == "admin"
    assert client.delete("/api/users/member-uid", headers=as_owner).json() == {"deleted": "member-uid"}


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/users", {"email": "new@example.test", "name": "New"}),
    ("post", "/api/users/member-uid/password-link", {}),
    ("post", "/api/users/app-uid/password-link", {}),
    ("post", "/api/users/app-uid/disable", {}),
    ("post", "/api/users/app-uid/enable", {}),
    ("delete", "/api/users/app-uid", None),
])
def test_a_sign_in_change_at_the_provider_says_where_users_are_managed(workspace, method, path, body):
    client, as_owner = workspace
    kwargs = {"headers": as_owner, **({"json": body} if body is not None else {})}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 409
    assert response.json()["detail"] == "Users are managed on the primary workspace; this deployment shares its sign-in pool"



def test_a_listed_member_signs_in_as_a_member_and_never_holds_the_admin_role(no_google_credential, monkeypatch, tmp_path):
    """INV_STUDIO_MEMBER_EMAILS is how access is given without identity admin: no invitation and no admin role."""
    from app.authentication import CloudIdentity
    from app.main import create_app
    from tests.conftest import audit_rows
    monkeypatch.setenv("INV_STUDIO_IDENTITY_ADMIN", "0")
    monkeypatch.setenv("INV_STUDIO_ALLOWED_EMAILS", "owner@example.test")
    monkeypatch.setenv("INV_STUDIO_MEMBER_EMAILS", " Joiner@Example.TEST ,owner@example.test,member@example.test,gone@example.test,new@example.test")
    people = {"joiner": {"uid": "joiner-uid", "email": "joiner@example.test"},
              "gone": {"uid": "gone-uid", "email": "gone@example.test"},
              "imposter": {"uid": "imposter-uid", "email": "member@example.test"},
              "unverified": {"uid": "new-uid", "email": "new@example.test", "email_verified": False}}
    identity = CloudIdentity()
    identity.client.verify_id_token = lambda token, check_revoked: {**CLAIMS, **people.get(token, {})}
    monkeypatch.setattr(httpx, "post", lambda url, json, **kwargs: httpx.Response(
        200, json={"users": [{"localId": {**CLAIMS, **people.get(json["idToken"], {})}["uid"], "validSince": "0"}]},
        request=httpx.Request("POST", url)))
    with TestClient(create_app(tmp_path / "d", identity=identity, store_type=Store)) as client:
        users = client.app.state.users
        def call(token, path="/api/session"):
            return client.get(path, headers={"Authorization": "Bearer " + token, **HEADERS})
        # The very first request is an admin route: it creates the member row and is refused.
        first = call("joiner", "/api/users")
        assert first.status_code == 403 and first.json()["detail"] == "Administrator access is required"
        assert call("joiner").json()["role"] == "member"
        assert [json.loads(payload)["role"] for event, payload in audit_rows(client.app) if event == "user.bootstrapped"] == ["member"]
        # On both lists: an administrator.
        assert call("owner").json()["role"] == "admin"
        # The existing refusals still hold for addresses on the member list.
        users.create("member-uid", "member@example.test", "", role="member", invited_by="owner@example.test", status="active", login_origin="existing")
        users.create("gone-uid", "gone@example.test", "", role="member", invited_by="owner@example.test", status="active", login_origin="existing")
        users.soft_delete("gone-uid")
        for token, refusal in (("gone", "removed from this workspace"), ("imposter", "registered under a different sign-in"),
                               ("unverified", "Verify your email to open this workspace")):
            response = call(token)
            assert response.status_code == 403 and refusal in response.json()["detail"], token
        assert users.get("imposter-uid") is None and users.get("new-uid") is None
