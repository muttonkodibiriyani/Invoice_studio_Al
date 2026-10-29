"""Independent review: status isolation and proposed administrative boundaries.

Permanent deletion and fine-rule feedback decisions are administrator-only, and the approver is the signed-in
administrator. Teaching from exported invoices stays a member action in v1 (exporting teaches too). All providers
and storage are local.
"""
import pytest
from fastapi import FastAPI

from app.authorization import ADMIN, PUBLIC, verify_registry
from tests.conftest import HEADERS, audit_rows, bearer
from tests.test_access import (
    ADMIN_ONLY, DISABLED, MIDDLEWARE_DENIALS, UNVERIFIED, api_routes, concrete,
)


@pytest.mark.parametrize("role", ["member", "admin"])
@pytest.mark.parametrize("status", ["invited", "active", "disabled"])
@pytest.mark.parametrize("verified", [False, True])
def test_each_route_with_fresh_role_status(role, status, verified, app, client, provider, monkeypatch):
    """Invited status is restored before EACH route, rather than lost after /session."""
    monkeypatch.setattr(app.state.limiter, "allow", lambda *a, **k: True)
    token = provider.seed("matrix-uid", "matrix@example.test", verified=verified)
    directory = app.state.users
    directory.create("matrix-uid", "matrix@example.test", "Matrix", role, "test", status=status, login_origin="existing")
    for method, path, level in api_routes(app):
        directory.update("matrix-uid", status=status, last_login_at=None)
        response = client.request(method, concrete(path), headers=bearer(token))
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        detail = data.get("detail") if isinstance(data, dict) else None
        expected = None
        if level != PUBLIC:
            if status == "disabled":
                expected = (403, DISABLED)
            elif not verified:
                expected = (403, UNVERIFIED)
            elif level == ADMIN and role != "admin":
                expected = (403, ADMIN_ONLY)
        if expected:
            assert (response.status_code, detail) == expected, (method, path, role, status, verified)
        else:
            assert response.status_code < 500, (method, path, response.status_code)
            assert not (response.status_code in (401, 403) and detail in MIDDLEWARE_DENIALS), (method, path)
        # Public requests must not authenticate/activate; protected requests activate only verified users.
        activates = level != PUBLIC and status == "invited" and verified
        assert directory.get("matrix-uid")["status"] == ("active" if activates else status), (method, path)


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


def test_revoked_admin_is_refused_before_a_mutation(client, provider, owner):
    provider.revoked.add("owner-uid")
    before = audit_rows(client.app)
    response = client.post("/api/users", headers=bearer(owner), json={"email": "new@example.test"})
    assert response.status_code == 401
    assert client.app.state.users.by_email("new@example.test") is None
    assert audit_rows(client.app) == before


@pytest.mark.parametrize("authorization", ["Bearer ", "Bearer invalid", "Basic invalid"])
def test_malformed_credentials_cannot_reach_protected_routes(authorization, app, client):
    for method, path, level in api_routes(app):
        if level != PUBLIC:
            response = client.request(method, concrete(path), headers={**HEADERS, "Authorization": authorization})
            assert response.status_code == 401, (method, path)
    assert app.state.users.list() == []


def test_unverified_bootstrap_identity_does_not_create_an_admin(client, provider):
    token = provider.seed("unverified-owner", "owner@example.test", verified=False)
    response = client.get("/api/session", headers=bearer(token))
    assert response.status_code == 403
    assert response.json()["detail"] == UNVERIFIED
    assert client.app.state.users.list() == []


@pytest.mark.parametrize("path", ["/api/users", "/api/password/forgot"])
def test_cross_origin_mutations_are_refused_before_side_effects(path, client, provider, owner):
    before = audit_rows(client.app)
    response = client.post(path, headers={**bearer(owner), "Origin": "https://attacker.example.test"},
                           json={"email": "new@example.test"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-origin requests are not accepted"
    assert client.app.state.users.by_email("new@example.test") is None
    assert audit_rows(client.app) == before


@pytest.fixture
def member(client, provider):
    token = provider.seed("review-member", "review-member@example.test")
    client.app.state.users.create("review-member", "review-member@example.test", "Member", "member", "test", status="active", login_origin="existing")
    return token


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


@pytest.mark.parametrize("status", ["active", "invited", "disabled"])
def test_forgot_password_does_not_disclose_status_or_issue_codes(status, client, provider, owner):
    provider.seed("forgot-uid", "forgot@example.test")
    client.app.state.users.create("forgot-uid", "forgot@example.test", "Forgot", "member", "test", status=status, login_origin="existing")
    known = client.post("/api/password/forgot", headers=HEADERS, json={"email": "forgot@example.test"})
    unknown = client.post("/api/password/forgot", headers=HEADERS, json={"email": "unknown@example.test"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert provider.codes == {}
    assert bool(client.app.state.users.get("forgot-uid")["reset_requested_at"]) == (status != "disabled")
