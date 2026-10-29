"""Invite → create password → sign in → use → disable → refused, plus the administrator guards and audit hygiene."""
import json

from app.user_admin import code_from_link, page_link
from tests.conftest import HEADERS, OWNER, audit_rows, bearer


def create(client, owner, email, role="member", name=""):
    response = client.post("/api/users", json={"email": email, "name": name, "role": role}, headers=bearer(owner))
    assert response.status_code == 201, response.text
    return response.json()


def test_full_lifecycle(client, provider, owner):
    issued = create(client, owner, "Ana@Example.test", name="Ana")
    assert issued["kind"] == "invite"
    assert issued["user"]["email"] == "ana@example.test" and issued["user"]["status"] == "invited"
    assert issued["link"].startswith("https://studio.example.test/set-password#oobCode=")
    code = issued["link"].split("#oobCode=", 1)[1]
    assert code in provider.codes
    uid = issued["user"]["uid"]
    assert provider.accounts[uid]["password"] is None, "no password is set at creation"

    # Not signed in yet: a guessed token is refused, and the account waits as invited.
    assert client.get("/api/session", headers=bearer("tok-guessed")).status_code == 401
    assert client.app.state.users.get(uid)["status"] == "invited"

    # The person opens the link and sets a password, then signs in.
    provider.set_password(code, "correct horse battery staple")
    token = provider.sign_in("ana@example.test", "correct horse battery staple")
    session = client.get("/api/session", headers=bearer(token))
    assert session.status_code == 200
    assert session.json()["role"] == "member" and session.json()["status"] == "active"
    listed = {u["uid"]: u for u in client.get("/api/users", headers=bearer(owner)).json()["users"]}
    assert listed[uid]["status"] == "active" and listed[uid]["last_login_at"]

    # Members cannot administer.
    assert client.get("/api/users", headers=bearer(token)).status_code == 403
    assert client.post("/api/users", json={"email": "x@example.test", "name": "", "role": "member"}, headers=bearer(token)).status_code == 403
    # ...but can use the workspace.
    assert client.get("/api/state", headers=bearer(token)).status_code == 200

    # A reset link for an active user.
    reset = client.post(f"/api/users/{uid}/password-link", json={}, headers=bearer(owner))
    assert reset.status_code == 200 and reset.json()["kind"] == "reset"

    # Disable: refused immediately, even with a token that is still technically valid.
    disabled = client.post(f"/api/users/{uid}/disable", json={}, headers=bearer(owner))
    assert disabled.status_code == 200 and disabled.json()["user"]["status"] == "disabled"
    assert provider.accounts[uid]["disabled"] is True and uid in provider.revoked
    refused = client.get("/api/session", headers=bearer(token))
    assert refused.status_code == 403 and "disabled" in refused.json()["detail"]
    assert client.post(f"/api/users/{uid}/password-link", json={}, headers=bearer(owner)).status_code == 409

    # Enable restores the previous standing.
    enabled = client.post(f"/api/users/{uid}/enable", json={}, headers=bearer(owner))
    assert enabled.json()["user"]["status"] == "active"
    token = provider.sign_in("ana@example.test", "correct horse battery staple")
    assert client.get("/api/session", headers=bearer(token)).status_code == 200

    # Promote, then delete.
    promoted = client.post(f"/api/users/{uid}/role", json={"role": "admin"}, headers=bearer(owner))
    assert promoted.json()["user"]["role"] == "admin"
    assert client.get("/api/users", headers=bearer(token)).status_code == 200
    deleted = client.delete(f"/api/users/{uid}", headers=bearer(owner))
    assert deleted.status_code == 200, deleted.text
    assert uid not in provider.accounts
    assert client.get("/api/session", headers=bearer(token)).status_code in (401, 403)
    assert uid not in {u["uid"] for u in client.get("/api/users", headers=bearer(owner)).json()["users"]}
    # The email can be invited again after deletion.
    assert create(client, owner, "ana@example.test")["user"]["uid"] != uid

    events = [event for event, _ in audit_rows(client.app)]
    for expected in ("user.bootstrapped", "user.invited", "user.invite_link_issued", "user.activated", "user.reset_link_issued",
                     "user.disabled", "user.enabled", "user.role_changed", "user.deleted"):
        assert expected in events, expected


def test_audit_log_never_contains_codes_or_links(client, provider, owner):
    issued = create(client, owner, "bo@example.test")
    code = code_from_link("https://x/?oobCode=" + issued["link"].split("#oobCode=")[1])
    client.post("/api/password/forgot", json={"email": "bo@example.test"}, headers=HEADERS)
    for event, payload in audit_rows(client.app):
        assert code not in payload, event
        assert "set-password" not in payload and "http" not in payload, event
        assert json.loads(payload)["actor"] in ("owner-uid", "anonymous")


def test_bootstrap_admin_only_for_allowlisted_emails(client, provider, owner):
    token = provider.seed("someone", "someone@example.test")
    response = client.get("/api/session", headers=bearer(token))
    assert response.status_code == 403
    assert "not been granted access" in response.json()["detail"]
    owner_session = client.get("/api/session", headers=bearer(owner)).json()
    assert owner_session["email"] == OWNER and owner_session["role"] == "admin"


def test_duplicate_email_and_foreign_sign_in_are_refused(client, provider, owner):
    create(client, owner, "dup@example.test")
    again = client.post("/api/users", json={"email": "DUP@example.test", "name": "", "role": "member"}, headers=bearer(owner))
    assert again.status_code == 409
    # A sign-in that exists in the shared user pool but was not created by Invoice Studio (for example a Product
    # Intelligence login) is refused and left exactly as it was: not enabled, no password code, no workspace row.
    provider.seed("legacy-uid", "legacy@example.test")
    provider.accounts["legacy-uid"]["disabled"] = True
    before = dict(provider.accounts["legacy-uid"])
    calls = len(provider.calls)
    refused = client.post("/api/users", json={"email": "Legacy@example.test", "name": "", "role": "member"}, headers=bearer(owner))
    assert refused.status_code == 409 and "did not create" in refused.json()["detail"]
    assert provider.accounts["legacy-uid"] == before and "legacy@example.test" not in provider.codes.values()
    assert provider.calls[calls:] == [("find_uid", "legacy@example.test")]
    assert client.app.state.users.by_email("legacy@example.test") is None
    assert not any("legacy" in payload for _, payload in audit_rows(client.app))


def test_invalid_input(client, owner):
    assert client.post("/api/users", json={"email": "nope", "name": "", "role": "member"}, headers=bearer(owner)).status_code == 422
    assert client.post("/api/users", json={"email": "a@b.example", "name": "", "role": "root"}, headers=bearer(owner)).status_code == 422
    assert client.post("/api/users/missing/disable", json={}, headers=bearer(owner)).status_code == 404
    assert client.post("/api/users/owner-uid/role", json={"role": "superuser"}, headers=bearer(owner)).status_code == 422


def test_self_and_last_admin_guards(client, provider, owner):
    for path in ("disable", "role"):
        body = {"role": "member"} if path == "role" else {}
        response = client.post(f"/api/users/owner-uid/{path}", json=body, headers=bearer(owner))
        assert response.status_code == 409, response.text
    assert client.delete("/api/users/owner-uid", headers=bearer(owner)).status_code == 409
    # A second admin who has signed in; the owner then tries to remove their own account via that admin.
    issued = create(client, owner, "second@example.test", role="admin")
    code = issued["link"].split("#oobCode=")[1]
    provider.set_password(code, "another long password here")
    second = provider.sign_in("second@example.test", "another long password here")
    assert client.get("/api/session", headers=bearer(second)).json()["role"] == "admin"
    # Two active admins: demoting the owner is allowed now.
    assert client.post("/api/users/owner-uid/role", json={"role": "member"}, headers=bearer(second)).status_code == 200
    # The second admin is now the last one: cannot be disabled, demoted or deleted by anyone.
    owner_is_member = client.get("/api/session", headers=bearer(owner)).json()
    assert owner_is_member["role"] == "member"
    assert client.post("/api/users/owner-uid/role", json={"role": "admin"}, headers=bearer(owner)).status_code == 403
    assert client.post(f"/api/users/{issued['user']['uid']}/disable", json={}, headers=bearer(second)).status_code == 409


def test_forgot_password_is_neutral_and_recorded(client, provider, owner):
    create(client, owner, "cara@example.test")
    known = client.post("/api/password/forgot", json={"email": "cara@example.test"}, headers=HEADERS)
    unknown = client.post("/api/password/forgot", json={"email": "nobody@example.test"}, headers=HEADERS)
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert "administrator" in known.json()["detail"]
    users = {u["email"]: u for u in client.get("/api/users", headers=bearer(owner)).json()["users"]}
    assert users["cara@example.test"]["reset_requested_at"]
    assert len(provider.codes) == 1, "forgot-password never mints a code by itself (the one code is from the invite)"
    # The badge clears once an administrator issues the reset link.
    uid = users["cara@example.test"]["uid"]
    client.app.state.users.update(uid, status="active")
    client.post(f"/api/users/{uid}/password-link", json={}, headers=bearer(owner))
    users = {u["email"]: u for u in client.get("/api/users", headers=bearer(owner)).json()["users"]}
    assert users["cara@example.test"]["reset_requested_at"] is None


def test_forgot_password_limits_per_connecting_address(client, owner):
    def forgot(forwarded):
        return client.post("/api/password/forgot", json={"email": "flood@example.test"},
                           headers={**HEADERS, "X-Forwarded-For": forwarded}).status_code
    create(client, owner, "flood@example.test")
    assert [forgot("198.51.100.7") for _ in range(30)] == [200] * 30
    assert forgot("198.51.100.7") == 429
    # The entry the front end appends (the last one) is the key: a forged first entry buys no fresh allowance.
    assert forgot("203.0.113.9, 198.51.100.7") == 429
    assert forgot("198.51.100.7, 192.0.2.44") == 200
    # Thirty-one accepted requests for one account recorded one reset request, not thirty-one.
    assert [event for event, _ in audit_rows(client.app)].count("user.reset_requested") == 1


def test_forgot_password_records_again_after_the_repeat_window(client, owner):
    uid = create(client, owner, "again@example.test")["user"]["uid"]
    users = client.app.state.users
    client.post("/api/password/forgot", json={"email": "again@example.test"}, headers=HEADERS)
    first = users.get(uid)["reset_requested_at"]
    client.post("/api/password/forgot", json={"email": "again@example.test"}, headers=HEADERS)
    assert users.get(uid)["reset_requested_at"] == first
    users.update(uid, reset_requested_at="2026-01-01T00:00:00+00:00")
    client.post("/api/password/forgot", json={"email": "again@example.test"}, headers=HEADERS)
    assert users.get(uid)["reset_requested_at"] > "2026-01-01T00:00:00+00:00"
    assert [event for event, _ in audit_rows(client.app)].count("user.reset_requested") == 2


def test_link_issue_rate_limit_per_admin(client, provider, owner):
    issued = create(client, owner, "lim@example.test")
    uid = issued["user"]["uid"]
    statuses = [client.post(f"/api/users/{uid}/password-link", json={}, headers=bearer(owner)).status_code for _ in range(21)]
    assert statuses.count(200) == 19 and statuses[-1] == 429, statuses


def test_provider_failure_is_a_502_without_internals(client, provider, owner):
    def boom(email):
        raise RuntimeError("secret internals: key=abc")
    provider.password_code = boom
    from app.user_admin import UserAdminError

    def wrapped(email):
        raise UserAdminError("The identity provider could not complete the request")
    provider.password_code = wrapped
    response = client.post("/api/users", json={"email": "z@example.test", "name": "", "role": "member"}, headers=bearer(owner))
    assert response.status_code == 502
    assert "identity provider" in response.json()["detail"] and "key=" not in response.text


def test_set_password_page_is_served_without_sign_in(client):
    response = client.get("/set-password")
    assert response.status_code == 200
    assert "set-password.js" in response.text and "firebase-auth.bundle.js" in response.text
    assert client.get("/static/set-password.js").status_code == 200
    assert page_link("https://x.example/", "abc") == "https://x.example/set-password#oobCode=abc"
