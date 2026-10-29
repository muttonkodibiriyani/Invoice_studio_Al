"""Entra ID sign-in (design doc "Entra ID sign-in design" §3-§5): the token checks, the role from the token on every
request, the read-only Users list, the entra_users records and their audit events, and that no Firebase path is left."""
import ast
import json
import time
from pathlib import Path

import jwt
import pytest

import app.authentication as authentication
from app.authentication import EntraIdentity
from tests.conftest import CLIENT_ID, HEADERS, ISSUER, OTHER_KEY, OWNER, TENANT_ID, audit_rows, bearer, entra_env

ROOT = Path(__file__).resolve().parents[1]
EXPIRED = "Sign-in expired or could not be verified. Sign in again."
NO_ACCESS = "This account has not been granted access to this workspace"
OTHER_GUID = "99999999-8888-4777-8666-555555555555"


def verify(identity, token):
    return identity.verify("Bearer " + token)


# ---- §3.2 token checks ----------------------------------------------------------------------------------------------

def test_a_valid_token_is_the_user_keyed_on_oid_with_the_role_from_the_token(identity, tenant):
    claims = verify(identity, tenant.token("oid-1", "Ana@Example.test"))
    assert claims == {"uid": "oid-1", "email": "ana@example.test", "name": "Ana", "role": "member"}
    assert verify(identity, tenant.token("oid-2", "b@example.test", roles=("member", "admin")))["role"] == "admin"


@pytest.mark.parametrize("claims", [
    {"iss": f"https://login.microsoftonline.com/{OTHER_GUID}/v2.0"},
    {"iss": f"https://sts.windows.net/{TENANT_ID}/"},  # the v1 issuer
    {"aud": OTHER_GUID},
    {"aud": f"api://{CLIENT_ID}"},  # the v1 audience form
    {"aud": [CLIENT_ID, OTHER_GUID]},
    {"tid": OTHER_GUID},
    {"exp": int(time.time()) - 120},
    {"nbf": int(time.time()) + 600},
    {"exp": None}, {"nbf": None}, {"iat": None},
    {"ver": "1.0"},
    {"scp": None}, {"scp": "User.Read"},
    {"azp": OTHER_GUID},
    {"oid": None}, {"oid": ""},
], ids=lambda c: ",".join(f"{k}={v}" for k, v in c.items()))
def test_a_token_failing_a_check_is_refused_as_not_signed_in(identity, tenant, claims):
    claims = dict(claims)
    token = tenant.token(claims.pop("oid", "oid-1"), "a@example.test", roles=("admin",), **claims)
    with pytest.raises(ValueError, match=EXPIRED):
        verify(identity, token)


def test_an_app_only_token_is_refused(identity, tenant):
    token = tenant.token("app-oid", roles=("admin",), scp=None, idtyp="app")
    with pytest.raises(ValueError, match=EXPIRED):
        verify(identity, token)


def test_the_expiry_leeway_is_sixty_seconds(identity, tenant):
    assert verify(identity, tenant.token("oid-1", "a@example.test", exp=int(time.time()) - 30))["uid"] == "oid-1"


@pytest.mark.parametrize("token", [
    "not.a.token",
    jwt.encode({"oid": "x"}, None, algorithm="none"),
    jwt.encode({"oid": "x", "aud": CLIENT_ID, "iss": ISSUER}, "a-shared-secret-of-thirty-two-bytes!", algorithm="HS256",
               headers={"kid": "test-kid"}),
], ids=["garbage", "alg-none", "hs256"])
def test_only_rs256_is_accepted(identity, token):
    with pytest.raises(ValueError, match=EXPIRED):
        verify(identity, token)


def test_a_token_signed_with_another_key_or_without_a_known_kid_is_refused(identity, tenant):
    for token in (tenant.token("oid-1", key=OTHER_KEY), tenant.token("oid-1", headers={"kid": "unknown"})):
        with pytest.raises(ValueError, match=EXPIRED):
            verify(identity, token)
    unsigned_kid = jwt.encode({"oid": "x"}, OTHER_KEY, algorithm="RS256")
    with pytest.raises(ValueError, match=EXPIRED):
        verify(identity, unsigned_kid)


def test_a_missing_or_malformed_header_or_an_oversized_token_is_refused(identity, tenant):
    for header in (None, "", "Basic abc", "bearer " + tenant.token("oid-1")):
        with pytest.raises(ValueError):
            identity.verify(header)
    with pytest.raises(ValueError, match=EXPIRED):
        identity.verify("Bearer " + "a" * 16385)


@pytest.mark.parametrize("roles", [(), ("reader",), ("Admin",)])
def test_a_token_without_an_app_role_is_forbidden_not_unauthenticated(identity, tenant, roles):
    with pytest.raises(PermissionError, match=NO_ACCESS):
        verify(identity, tenant.token("oid-1", "a@example.test", roles=roles))
    with pytest.raises(PermissionError, match=NO_ACCESS):
        verify(identity, tenant.token("oid-1", "a@example.test", roles=None))


# ---- signing keys ---------------------------------------------------------------------------------------------------

def test_the_tenant_keys_are_fetched_once_from_the_tenant_and_cached(identity, tenant):
    for n in range(3):
        verify(identity, tenant.token(f"oid-{n}"))
    assert tenant.fetched == [f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"]


def test_an_unknown_kid_refetches_at_most_once_per_five_minutes(identity, tenant, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(authentication.time, "monotonic", lambda: clock[0])
    verify(identity, tenant.token("oid-1"))
    unknown = tenant.token("oid-1", headers={"kid": "rotated"})
    for _ in range(3):
        with pytest.raises(ValueError):
            verify(identity, unknown)
    assert len(tenant.fetched) == 1  # inside five minutes of the first fetch: no refetch
    clock[0] += 301
    with pytest.raises(ValueError):
        verify(identity, unknown)
    with pytest.raises(ValueError):
        verify(identity, unknown)
    assert len(tenant.fetched) == 2


def test_the_keys_are_refreshed_after_a_day(identity, tenant, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(authentication.time, "monotonic", lambda: clock[0])
    verify(identity, tenant.token("oid-1"))
    clock[0] += 86401
    verify(identity, tenant.token("oid-1"))
    assert len(tenant.fetched) == 2


def test_no_keys_means_no_sign_in(tenant, monkeypatch, caplog):
    entra_env(monkeypatch)

    def unreachable(url):
        raise OSError("login.microsoftonline.com unreachable")
    identity = EntraIdentity(fetch=unreachable)
    with pytest.raises(ValueError, match=EXPIRED):
        verify(identity, tenant.token("oid-1"))
    assert "signing keys" in caplog.text


# ---- §3.1 settings --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, value", [
    ("INV_STUDIO_ENTRA_TENANT_ID", ""), ("INV_STUDIO_ENTRA_TENANT_ID", "common"),
    ("INV_STUDIO_ENTRA_TENANT_ID", "organizations"), ("INV_STUDIO_ENTRA_TENANT_ID", "consumers"),
    ("INV_STUDIO_ENTRA_TENANT_ID", "contoso.onmicrosoft.com"), ("INV_STUDIO_ENTRA_CLIENT_ID", ""),
    ("INV_STUDIO_ENTRA_CLIENT_ID", "api://" + CLIENT_ID),
])
def test_cloud_mode_refuses_to_start_without_a_single_tenant_and_client_guid(monkeypatch, name, value):
    entra_env(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        EntraIdentity()


@pytest.mark.parametrize("name", ["GOOGLE_CLOUD_PROJECT", "INV_STUDIO_FIREBASE_API_KEY", "INV_STUDIO_FIREBASE_APP_ID",
                                  "INV_STUDIO_ALLOWED_EMAILS", "INV_STUDIO_MEMBER_EMAILS"])
def test_cloud_mode_refuses_to_start_with_a_firebase_or_bootstrap_setting_left_over(monkeypatch, name):
    entra_env(monkeypatch)
    monkeypatch.setenv(name, "left-over")
    with pytest.raises(ValueError, match=name):
        EntraIdentity()


def test_local_mode_needs_no_entra_settings(monkeypatch):
    for name in ("INV_STUDIO_CLOUD", "INV_STUDIO_ENTRA_TENANT_ID", "INV_STUDIO_ENTRA_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    identity = EntraIdentity()
    assert identity.cloud is False and identity.config() == {"cloud": False}


# ---- the app --------------------------------------------------------------------------------------------------------

def test_public_config_carries_the_public_entra_values_only(client):
    assert client.get("/api/public-config").json() == {"cloud": True, "entra": {
        "clientId": CLIENT_ID, "tenantId": TENANT_ID,
        "authority": f"https://login.microsoftonline.com/{TENANT_ID}",
        "scopes": [f"api://{CLIENT_ID}/access_as_user"]}}


def test_the_cloud_csp_allows_entra_and_nothing_from_google(client):
    csp = client.get("/health").headers["Content-Security-Policy"]
    assert "script-src 'self';" in csp
    assert "connect-src 'self' https://login.microsoftonline.com;" in csp
    assert "frame-src 'self' blob: https://login.microsoftonline.com;" in csp
    assert "google" not in csp and "img-src 'self' data: blob:;" in csp


def test_first_sign_in_records_the_user_and_the_audit_actor_is_the_oid(client, tenant):
    session = client.get("/api/session", headers=bearer(tenant.token("oid-ana", "ana@example.test"))).json()
    assert session == {"uid": "oid-ana", "email": "ana@example.test", "name": "ana", "role": "member"}
    [row] = client.app.state.users.list()
    assert row["oid"] == "oid-ana" and row["tenant_id"] == TENANT_ID and row["role"] == "member"
    assert row["first_login_at"] == row["last_login_at"]
    [(event, payload)] = [r for r in audit_rows(client.app) if r[0].startswith("user.")]
    assert event == "user.first_sign_in"
    assert json.loads(payload) == {"oid": "oid-ana", "email": "ana@example.test", "role": "member", "actor": "oid-ana"}
    client.get("/api/session", headers=bearer(tenant.token("oid-ana", "ana@example.test")))
    assert [r[0] for r in audit_rows(client.app) if r[0].startswith("user.")] == ["user.first_sign_in"]


def test_a_user_removed_from_the_group_is_refused_on_their_next_token(client, tenant, owner):
    assert client.get("/api/users", headers=bearer(owner)).status_code == 200
    removed = tenant.token("owner-uid", OWNER, roles=())
    for method, path in (("GET", "/api/session"), ("GET", "/api/users"), ("POST", "/api/settings")):
        response = client.request(method, path, headers=bearer(removed), json={})
        assert (response.status_code, response.json()["detail"]) == (403, NO_ACCESS), path
    assert client.app.state.users.get("owner-uid")["role"] == "admin"  # the record stays, and grants nothing


def test_the_row_never_grants_the_role_the_token_decides(client, tenant, owner):
    demoted = tenant.token("owner-uid", OWNER, roles=("member",))
    assert client.get("/api/session", headers=bearer(demoted)).json()["role"] == "member"
    response = client.get("/api/users", headers=bearer(demoted))
    assert (response.status_code, response.json()["detail"]) == (403, "Administrator access is required")
    assert client.app.state.users.get("owner-uid")["role"] == "member"  # a role change is written at once
    changes = [json.loads(p) for e, p in audit_rows(client.app) if e == "user.role_observed_changed"]
    assert changes == [{"oid": "owner-uid", "email": OWNER, "from": "admin", "to": "member", "actor": "owner-uid"}]


def test_the_users_list_is_read_only_and_names_the_groups(client, tenant, owner, monkeypatch):
    client.get("/api/session", headers=bearer(tenant.token("oid-m", "m@example.test")))
    body = client.get("/api/users", headers=bearer(owner)).json()
    assert body["groups"] == {"admin": "", "member": ""}
    assert {(u["oid"], u["email"], u["role"]) for u in body["users"]} == {
        ("owner-uid", OWNER, "admin"), ("oid-m", "m@example.test", "member")}
    assert set(body["users"][0]) == {"oid", "tenant_id", "email", "name", "role", "first_login_at", "last_login_at"}
    member = tenant.token("oid-m", "m@example.test")
    assert client.get("/api/users", headers=bearer(member)).status_code == 403


def test_the_group_display_names_come_from_settings(tmp_path, tenant, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.store import Store
    entra_env(monkeypatch)
    monkeypatch.setenv("INV_STUDIO_ENTRA_ADMIN_GROUP_NAME", "Invoice Studio admins")
    monkeypatch.setenv("INV_STUDIO_ENTRA_MEMBER_GROUP_NAME", "Invoice Studio members")
    app = create_app(tmp_path / "d", identity=EntraIdentity(fetch=tenant.jwks), store_type=Store)
    with TestClient(app) as c:
        groups = c.get("/api/users", headers=bearer(tenant.token("o", OWNER, roles=("admin",)))).json()["groups"]
    assert groups == {"admin": "Invoice Studio admins", "member": "Invoice Studio members"}


@pytest.mark.parametrize("method, path", [
    ("POST", "/api/users"), ("POST", "/api/users/x/password-link"), ("POST", "/api/users/x/disable"),
    ("POST", "/api/users/x/enable"), ("POST", "/api/users/x/role"), ("DELETE", "/api/users/x"),
    ("POST", "/api/password/forgot"), ("GET", "/set-password"),
])
def test_invites_password_links_and_user_changes_are_gone(client, owner, method, path):
    response = client.request(method, path, headers=bearer(owner), json={"email": "x@example.test", "role": "member"})
    assert response.status_code in (404, 405), (method, path)


def test_the_keys_are_fetched_once_for_many_requests(client, tenant, owner):
    for _ in range(3):
        client.get("/api/state", headers=bearer(owner))
        client.post("/api/settings", headers=bearer(owner), json={})
    assert len(tenant.fetched) == 1


# ---- no Firebase path left (design §0.2: no provider switch) --------------------------------------------------------

def test_no_module_under_app_imports_firebase():
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n.split(".")[0] in ("firebase_admin", "firebase") for n in names), path
    assert not (ROOT / "app/user_admin.py").exists()


def test_firebase_admin_is_no_dependency():
    assert "firebase" not in (ROOT / "pyproject.toml").read_text().casefold()
    assert "firebase" not in (ROOT / "constraints.txt").read_text().casefold()


def test_no_provider_switch_setting_exists():
    for path in (ROOT / "app").rglob("*.py"):
        assert "INV_STUDIO_AUTH_PROVIDER" not in path.read_text(), path


def test_unsigned_requests_never_reach_protected_routes(client):
    assert client.get("/api/session", headers=HEADERS).json()["detail"] == "Sign in to Invoice Studio"


def test_ids_pasted_in_upper_case_still_match_the_lower_case_claims(tenant, monkeypatch):
    entra_env(monkeypatch)
    monkeypatch.setenv("INV_STUDIO_ENTRA_TENANT_ID", TENANT_ID.upper())
    monkeypatch.setenv("INV_STUDIO_ENTRA_CLIENT_ID", " " + CLIENT_ID.upper())
    identity = EntraIdentity(fetch=tenant.jwks)
    assert verify(identity, tenant.token("oid-1", "a@example.test"))["uid"] == "oid-1"
    assert tenant.fetched == [f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"]
    assert identity.config()["entra"]["clientId"] == CLIENT_ID


@pytest.mark.parametrize("claims, reason", [
    ({"ver": "1.0"}, "audience, tenant or version"),
    ({"scp": "User.Read"}, "not a delegated token from this app"),
    ({"aud": f"api://{CLIENT_ID}"}, "InvalidAudienceError"),
    ({"exp": int(time.time()) - 600}, "ExpiredSignatureError"),
])
def test_a_refused_token_logs_which_check_refused_it_and_nothing_of_the_token(identity, tenant, caplog, claims, reason):
    token = tenant.token("secret-oid", "secret@example.test", **claims)
    with caplog.at_level("INFO", logger="inv_studio.auth"), pytest.raises(ValueError, match=EXPIRED):
        verify(identity, token)
    [record] = [r for r in caplog.records if r.name == "inv_studio.auth"]
    assert record.levelname == "INFO" and record.getMessage() == "sign-in token refused: " + reason
    assert token not in caplog.text and "secret" not in caplog.text and TENANT_ID not in caplog.text
