"""Shared fixtures: the app in cloud mode with a fake identity provider and the SQLite store.

No Firebase, no PostgreSQL. Tokens are the string "tok-<uid>"; password codes are issued and consumed by FakeUserAdmin.
"""
import os
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("INV_STUDIO_LEARN", "0")
os.environ.pop("INV_STUDIO_CLOUD", None)
os.environ.pop("INV_STUDIO_DEMO_REFERENCES", None)

from app.main import create_app  # noqa: E402
from app.store import Store  # noqa: E402
from app.user_admin import UserAdminError  # noqa: E402

OWNER = "owner@example.test"
HEADERS = {"X-Studio-Request": "1"}


class FakeUserAdmin:
    """The identity provider's user store, as far as the app can see it."""

    def __init__(self):
        self.accounts = {}   # uid -> {"email", "name", "disabled", "password"}
        self.codes = {}      # oobCode -> email
        self.revoked = set()
        self.calls = []

    def _uid_for(self, email):
        for uid, account in self.accounts.items():
            if account["email"] == email:
                return uid
        return None

    def find_uid(self, email):
        self.calls.append(("find_uid", email))
        return self._uid_for(email)

    def create(self, email, name):
        self.calls.append(("create", email))
        if self._uid_for(email):
            raise UserAdminError("An account with this email already exists in the identity provider")
        uid = "u" + secrets.token_hex(6)
        self.accounts[uid] = {"email": email, "name": name, "disabled": False, "password": None}
        return uid

    def password_code(self, email):
        self.calls.append(("password_code", email))
        if not self._uid_for(email):
            raise UserAdminError("The identity provider has no account for this user")
        code = "code-" + secrets.token_urlsafe(18)
        self.codes[code] = email
        return code

    def set_disabled(self, uid, disabled):
        self.calls.append(("set_disabled", uid, disabled))
        if uid not in self.accounts:
            raise UserAdminError("The identity provider has no account for this user")
        self.accounts[uid]["disabled"] = disabled
        if disabled:
            self.revoked.add(uid)

    def revoke(self, uid):
        self.calls.append(("revoke", uid))
        self.revoked.add(uid)

    def delete(self, uid):
        self.calls.append(("delete", uid))
        if uid not in self.accounts:
            raise UserAdminError("The identity provider has no account for this user")
        del self.accounts[uid]

    # What the browser does on the set-password page, and at sign-in.
    def set_password(self, code, password):
        email = self.codes.pop(code)  # a code works once
        self.accounts[self._uid_for(email)]["password"] = password

    def sign_in(self, email, password):
        uid = self._uid_for(email)
        account = self.accounts.get(uid)
        if account is None or account["disabled"] or account["password"] != password:
            raise ValueError("wrong credentials")
        self.revoked.discard(uid)
        return "tok-" + uid

    # Test helper: an account that exists in the provider but was never created through the app.
    def seed(self, uid, email, verified=True):
        self.accounts[uid] = {"email": email, "name": "", "disabled": False, "password": "pw", "verified": verified}
        return "tok-" + uid


class FakeIdentity:
    def __init__(self, admin, bootstrap_admins=(OWNER,), public_url="https://studio.example.test"):
        self.cloud = True
        self.project = "test-project"
        self.bootstrap_admins = {x.casefold() for x in bootstrap_admins}
        self.bootstrap_members = set()
        self.tenant_id = ""
        self.public_url = public_url
        self.admin = admin
        self.verify_calls = []

    def config(self):
        return {"cloud": True, "firebase": {"providers": ["password"]}}

    def verify(self, authorization, check_revoked=False):
        self.verify_calls.append((authorization, check_revoked))
        if not authorization or not authorization.startswith("Bearer "):
            raise ValueError("Sign in to Invoice Studio")
        token = authorization[7:]
        if not token.startswith("tok-"):
            raise ValueError("Sign-in expired or could not be verified. Sign in again.")
        uid = token[4:]
        account = self.admin.accounts.get(uid)
        if account is None:
            raise ValueError("Sign-in expired or could not be verified. Sign in again.")
        if check_revoked and uid in self.admin.revoked:
            raise ValueError("Sign-in expired or could not be verified. Sign in again.")
        return {"email": account["email"], "uid": uid, "email_verified": account.get("verified", True)}


@pytest.fixture
def provider():
    return FakeUserAdmin()


@pytest.fixture
def identity(provider):
    return FakeIdentity(provider)


@pytest.fixture
def app(tmp_path, identity):
    return create_app(tmp_path / "data", identity=identity, store_type=Store)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def bearer(token):
    return {"Authorization": "Bearer " + token, **HEADERS}


@pytest.fixture
def owner(provider, client):
    """The bootstrap administrator, signed in once so the admin row exists."""
    token = provider.seed("owner-uid", OWNER)
    response = client.get("/api/session", headers=bearer(token))
    assert response.status_code == 200, response.text
    return token


def audit_rows(app):
    with app.state.store.connection() as c:
        return [(r["event"], r["payload"]) for r in c.execute("SELECT event,payload FROM audit ORDER BY id")]
