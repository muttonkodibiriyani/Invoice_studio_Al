"""Independent review: status isolation and proposed administrative boundaries.

Permanent deletion and fine-rule feedback decisions are administrator-only, and the approver is the signed-in
administrator. Teaching from exported invoices stays a member action in v1 (exporting teaches too). All providers
and storage are local.
"""
import pytest
from fastapi import FastAPI

from app.authorization import ADMIN, PUBLIC, verify_registry
from tests.conftest import HEADERS, audit_rows, bearer
from tests.test_access import ADMIN_ONLY, MIDDLEWARE_DENIALS, NO_ACCESS, api_routes, concrete


@pytest.mark.parametrize("token_roles", [(), ("member",), ("admin",), ("member", "admin")])
@pytest.mark.parametrize("row_role", [None, "member", "admin"])
def test_each_route_follows_the_token_role_whatever_the_row_says(token_roles, row_role, app, client, tenant, monkeypatch):
    """The entra_users row is a record, never a grant: the token's role decides on every request."""
    monkeypatch.setattr(app.state.limiter, "allow", lambda *a, **k: True)
    if row_role:
        earlier = tenant.token("matrix-uid", "matrix@example.test", roles=(row_role,))
        assert client.get("/api/session", headers=bearer(earlier)).status_code == 200
    token = tenant.token("matrix-uid", "matrix@example.test", roles=token_roles)
    for method, path, level in api_routes(app):
        response = client.request(method, concrete(path), headers=bearer(token))
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        detail = data.get("detail") if isinstance(data, dict) else None
        expected = None
        if level != PUBLIC:
            if not token_roles:
                expected = (403, NO_ACCESS)
            elif level == ADMIN and "admin" not in token_roles:
                expected = (403, ADMIN_ONLY)
        if expected:
            assert (response.status_code, detail) == expected, (method, path, token_roles, row_role)
        else:
            assert response.status_code < 500, (method, path, response.status_code)
            assert not (response.status_code in (401, 403) and detail in MIDDLEWARE_DENIALS), (method, path)


def test_registry_rejects_a_new_unmarked_api_route():
    application = FastAPI()

    @application.get("/api/forgotten")
    def unmarked():
        return {}

    with pytest.raises(RuntimeError, match="/api/forgotten"):
        verify_registry(application)


def test_runtime_denies_unmarked_route_even_after_startup(app, client, owner):
    @app.get("/api/unmarked-after-startup")
    def unmarked():
        pytest.fail("an unregistered handler must never run")

    for headers in ({}, bearer(owner)):
        response = client.get("/api/unmarked-after-startup", headers=headers)
        assert response.status_code == 404
        assert response.json()["detail"] == "Not found"


@pytest.mark.parametrize("authorization", ["Bearer ", "Bearer invalid", "Basic invalid"])
def test_malformed_credentials_cannot_reach_protected_routes(authorization, app, client):
    for method, path, level in api_routes(app):
        if level != PUBLIC:
            response = client.request(method, concrete(path), headers={**HEADERS, "Authorization": authorization})
            assert response.status_code == 401, (method, path)
    assert app.state.users.list() == []


def test_cross_origin_mutations_are_refused_before_side_effects(client, owner):
    before = audit_rows(client.app)
    settings = client.get("/api/state", headers=bearer(owner)).json()["settings"]
    response = client.post("/api/settings", headers={**bearer(owner), "Origin": "https://attacker.example.test"},
                           json={})
    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-origin requests are not accepted"
    assert client.get("/api/state", headers=bearer(owner)).json()["settings"] == settings
    assert audit_rows(client.app) == before


@pytest.fixture
def member(tenant):
    return tenant.token("review-member", "review-member@example.test")


def test_member_cannot_permanently_delete_shared_invoice(client, member):
    store = client.app.state.store
    source = store.root / "uploads" / "review-fixture.pdf"
    source.write_bytes(b"synthetic invoice")
    store.job("review-fixture", {"id": "review-fixture", "revision": 1, "status": "review", "path": str(source)})
    response = client.post("/api/jobs/delete", headers=bearer(member), json={
        "jobs": [{"id": "review-fixture", "revision": 1}], "confirm_permanent": True,
    })
    assert response.status_code == 403
    assert store.job("review-fixture") is not None
    assert source.exists()


def propose_feedback(client, token):
    response = client.post("/api/fine-rules/feedback", headers=bearer(token), json={
        "invoice_line": "synthetic invoice line", "correction": "reviewed correction", "evidence": "synthetic evidence",
    })
    assert response.status_code == 200
    return response.json()["Feedback ID"]


def test_member_cannot_approve_their_own_feedback(client, member):
    fid = propose_feedback(client, member)
    response = client.post(f"/api/fine-rules/feedback/{fid}/decision", headers=bearer(member), json={
        "approver": "Workspace owner", "decision": "Approved", "version": "review",
    })
    assert response.status_code == 403
    entries = client.get("/api/fine-rules/feedback", headers=bearer(member)).json()
    assert next(row for row in entries if row["Feedback ID"] == fid)["Decision"] != "Approved"


def test_feedback_approver_comes_from_authenticated_identity(client, owner):
    fid = propose_feedback(client, owner)
    response = client.post(f"/api/fine-rules/feedback/{fid}/decision", headers=bearer(owner), json={
        "approver": "Someone else", "decision": "Approved", "version": "review",
    })
    assert response.status_code == 200
    assert response.json()["Approver"] == "owner@example.test"


@pytest.mark.parametrize("path", ["/api/jobs/delete", "/api/fine-rules/feedback/{fid}/decision"])
def test_destructive_and_approval_routes_declare_admin(app, path):
    levels = {(method, route): level for method, route, level in api_routes(app)}
    assert levels[("POST", path)] == ADMIN
