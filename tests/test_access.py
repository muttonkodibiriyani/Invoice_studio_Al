"""Every API route × every principal: nobody gets past the middleware without the right role and status."""
import pytest
from starlette.routing import Route

from app.authorization import ADMIN, MEMBER, PUBLIC, route_access
from tests.conftest import HEADERS, bearer

NOT_SIGNED_IN = "Sign in to Invoice Studio"
NO_ACCESS = "This account has not been granted access to this workspace"
DISABLED = "This account is disabled. Contact the workspace administrator."
UNVERIFIED = "Verify your email to open this workspace"
ADMIN_ONLY = "Administrator access is required"
MIDDLEWARE_DENIALS = {NOT_SIGNED_IN, NO_ACCESS, DISABLED, UNVERIFIED, ADMIN_ONLY, "Not found", "Missing request protection header"}


def api_routes(app):
    routes = []
    for route in app.router.routes:
        if isinstance(route, Route) and route.path.startswith("/api/"):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                routes.append((method, route.path, route_access(app, {"type": "http", "method": method, "path": route.path.replace("{", "x").replace("}", "")})))
    assert routes
    return routes


def concrete(path):
    return "".join("x" if part.startswith("{") else part for part in path.replace("{", "|{").replace("}", "}|").split("|"))


def make_principals(provider, client, owner):
    member_token = provider.seed("member-uid", "member@example.test")
    invited_token = provider.seed("invited-uid", "invited@example.test")
    disabled_token = provider.seed("disabled-uid", "disabled@example.test")
    unverified_token = provider.seed("unverified-uid", "unverified@example.test", verified=False)
    unknown_token = provider.seed("stranger-uid", "stranger@example.test")
    users = client.app.state.users
    users.create("member-uid", "member@example.test", "M", "member", invited_by="test", status="active", login_origin="existing")
    users.create("invited-uid", "invited@example.test", "I", "member", invited_by="test", status="invited", login_origin="existing")
    users.create("disabled-uid", "disabled@example.test", "D", "admin", invited_by="test", status="disabled", login_origin="existing")
    users.create("unverified-uid", "unverified@example.test", "U", "member", invited_by="test", status="active", login_origin="existing")
    return {
        "anonymous": None,
        "unknown": unknown_token,
        "disabled": disabled_token,
        "unverified": unverified_token,
        "member": member_token,
        "invited": invited_token,
        "admin": owner,
    }


EXPECTED = {
    # principal -> {level: expected (status, detail) or None meaning "must reach the handler"}
    "anonymous": {PUBLIC: None, MEMBER: (401, NOT_SIGNED_IN), ADMIN: (401, NOT_SIGNED_IN)},
    "unknown": {PUBLIC: None, MEMBER: (403, NO_ACCESS), ADMIN: (403, NO_ACCESS)},
    "disabled": {PUBLIC: None, MEMBER: (403, DISABLED), ADMIN: (403, DISABLED)},
    "unverified": {PUBLIC: None, MEMBER: (403, UNVERIFIED), ADMIN: (403, UNVERIFIED)},
    "member": {PUBLIC: None, MEMBER: None, ADMIN: (403, ADMIN_ONLY)},
    "invited": {PUBLIC: None, MEMBER: None, ADMIN: (403, ADMIN_ONLY)},
    "admin": {PUBLIC: None, MEMBER: None, ADMIN: None},
}


def test_every_api_route_declares_an_access_level(app):
    for method, path, level in api_routes(app):
        assert level in (PUBLIC, MEMBER, ADMIN), (method, path)


def test_admin_surface_is_admin_only(app):
    admin_prefixes = ("/api/users", "/api/connections", "/api/subscriptions", "/api/chatgpt", "/api/settings")
    for method, path, level in api_routes(app):
        if path.startswith(admin_prefixes):
            assert level == ADMIN, (method, path)
        if method in ("POST", "PUT", "DELETE") and path.startswith(("/api/policy", "/api/templates", "/api/fine-rules/config", "/api/target-export/config", "/api/references", "/api/demo")):
            assert level == ADMIN, (method, path)
        if method == "DELETE" and path.startswith("/api/learned"):
            assert level == ADMIN, (method, path)  # members teach from exported invoices; only admins forget
    public = {(m, p) for m, p, level in api_routes(app) if level == PUBLIC}
    assert public == {("GET", "/api/public-config"), ("POST", "/api/password/forgot")}


@pytest.mark.parametrize("principal", list(EXPECTED))
def test_route_matrix(principal, app, client, provider, owner):
    app.state.limiter.allow = lambda *a, **k: True
    tokens = make_principals(provider, client, owner)
    token = tokens[principal]
    failures = []
    for method, path, level in api_routes(app):
        headers = bearer(token) if token else dict(HEADERS)
        response = client.request(method, concrete(path), headers=headers)
        expected = EXPECTED[principal][level]
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        detail = body.get("detail") if isinstance(body, dict) else None
        if expected is None:
            # Must have passed the guard: anything the handler itself returns is fine, a middleware denial is not.
            if response.status_code in (401, 403) and detail in MIDDLEWARE_DENIALS:
                failures.append((method, path, response.status_code, detail))
            if response.status_code == 404 and detail == "Not found":
                failures.append((method, path, "unregistered route"))
        elif (response.status_code, detail) != expected:
            failures.append((method, path, response.status_code, detail, "expected", expected))
    assert not failures, failures


def test_unregistered_api_path_is_404_without_a_token(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "Not found"
    response = client.post("/api/does-not-exist", headers=HEADERS)
    assert response.status_code == 404


def test_state_changing_calls_need_the_request_header(client, owner):
    response = client.post("/api/users", json={"email": "a@example.test", "name": "", "role": "member"}, headers={"Authorization": "Bearer " + owner})
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing request protection header"


def test_revocation_is_checked_on_sensitive_calls_only(client, identity, owner):
    identity.verify_calls.clear()
    client.get("/api/session", headers=bearer(owner))
    client.get("/api/users", headers=bearer(owner))
    client.post("/api/password/forgot", json={"email": "x@example.test"}, headers=HEADERS)
    checks = {call[1] for call in identity.verify_calls}
    member_get, admin_get = identity.verify_calls[0][1], identity.verify_calls[1][1]
    assert member_get is False and admin_get is True
    assert len(identity.verify_calls) == 2, "public routes never call the identity provider"
    assert checks == {False, True}


def test_docs_are_off_in_the_cloud(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_docs_stay_on_locally(tmp_path):
    from app.main import create_app
    from app.store import Store

    class Local:
        cloud = False
        bootstrap_admins = set()
        admin = None
        public_url = ""
        def config(self): return {"cloud": False}
        def verify(self, *a, **k): raise AssertionError("not called locally")
    from fastapi.testclient import TestClient
    with TestClient(create_app(tmp_path / "d", identity=Local(), store_type=Store), client=("127.0.0.1", 50000)) as c:
        assert c.get("/docs").status_code == 200
        assert c.get("/api/session").json()["role"] == "admin"
        assert c.get("/api/users").status_code == 409


def test_local_mode_refuses_other_computers_whatever_the_host_header(tmp_path, monkeypatch):
    """Local mode makes every caller the administrator, so a caller on the LAN must not get in by sending Host: localhost."""
    monkeypatch.delenv("INV_STUDIO_CLOUD", raising=False)
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.store import Store
    app = create_app(tmp_path / "d", store_type=Store)
    assert not app.state.identity.cloud
    with TestClient(app, base_url="http://localhost", client=("192.168.1.50", 50000)) as c:
        assert c.get("/api/session").status_code == 403
        assert c.get("/api/state").status_code == 403
        assert c.get("/").status_code == 403
        assert c.post("/api/chatgpt/export", headers=HEADERS).status_code == 403
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/session").json()["role"] == "admin"


def test_security_headers_and_request_id(client):
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert len(response.headers["X-Request-Id"]) >= 8
    response = client.get("/health", headers={"X-Cloud-Trace-Context": "abcdef0123456789abcdef0123456789/123;o=1"})
    assert response.headers["X-Request-Id"] == "abcdef0123456789abcdef0123456789"


def test_cloud_mode_sends_hsts_on_every_response(client):
    # Security pack N2: a normal response, an error the middleware answers, an error a route raises, a static file.
    for path, status in (("/health", 200), ("/api/state", 401), ("/api/no-such-route", 404), ("/static/app.js", 200)):
        response = client.get(path)
        assert response.status_code == status, path
        assert response.headers["Strict-Transport-Security"] == "max-age=31536000", path
    response = client.get("/health", headers={"Host": "elsewhere.example.test"})
    assert response.status_code == 400
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_local_mode_sends_no_hsts(tmp_path, monkeypatch):
    # Local mode is plain http on loopback: an HSTS header there would lock the browser out of it.
    monkeypatch.delenv("INV_STUDIO_CLOUD", raising=False)
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.store import Store
    app = create_app(tmp_path / "d", store_type=Store)
    assert not app.state.identity.cloud
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        for path, status in (("/health", 200), ("/api/no-such-route", 404), ("/static/app.js", 200)):
            response = c.get(path)
            assert response.status_code == status, path
            assert "Strict-Transport-Security" not in response.headers, path
    with TestClient(app, base_url="http://localhost", client=("192.168.1.50", 50000)) as c:
        response = c.get("/api/state")
        assert response.status_code == 403
        assert "Strict-Transport-Security" not in response.headers


def test_extra_hosts_are_accepted_in_cloud_mode_only_when_configured(tmp_path, identity, monkeypatch):
    # Azure tests a revision on its own address (<app>--<suffix>.<environment domain>) before it gets traffic.
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.store import Store
    monkeypatch.setenv("INV_STUDIO_ALLOWED_ORIGINS", "https://inv.happy-sea.westeurope.azurecontainerapps.io")
    revision = "inv--r1a2b3c.happy-sea.westeurope.azurecontainerapps.io"
    app = create_app(tmp_path / "a", identity=identity, store_type=Store)
    with TestClient(app, base_url="http://" + revision) as c:
        assert c.get("/health").status_code == 400
    monkeypatch.setenv("INV_STUDIO_ALLOWED_HOSTS", "*.happy-sea.westeurope.azurecontainerapps.io")
    app = create_app(tmp_path / "b", identity=identity, store_type=Store)
    for host, status in ((revision, 200), ("inv.happy-sea.westeurope.azurecontainerapps.io", 200),
                         ("10.0.2.15:8080", 400), ("evil.example.com", 400)):
        with TestClient(app, base_url="http://" + host) as c:
            assert c.get("/health").status_code == status, host


ROUTE_TABLE = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
               "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
               "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n")


@pytest.mark.parametrize(("files", "gateway"), [
    ({"/.dockerenv": "", "/proc/net/route": ROUTE_TABLE}, "172.17.0.1"),
    ({"/proc/net/route": ROUTE_TABLE}, None),  # not in a container
    ({"/.dockerenv": ""}, None),  # route table unreadable
])
def test_the_container_gateway_is_read_from_the_default_route(monkeypatch, tmp_path, files, gateway):
    import app.authorization as authorization
    for name, text in files.items():
        (tmp_path / name.strip("/").replace("/", "_")).write_text(text)
    monkeypatch.setattr(authorization, "Path", lambda name: tmp_path / name.strip("/").replace("/", "_"))
    authorization.container_gateway.cache_clear()
    try:
        assert authorization.container_gateway() == gateway
    finally:
        authorization.container_gateway.cache_clear()
