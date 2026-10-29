"""End to end against the real Firebase Auth emulator: the app's own CloudIdentity and FirebaseUserAdmin (no fakes),
and the same Identity Toolkit calls the browser makes on the set-password page and at sign-in.

Local only: a demo- project on 127.0.0.1, never a real project. Needs the Firebase CLI (firebase-tools); skipped
without it. Set FIREBASE_AUTH_EMULATOR_HOST to reuse an emulator that is already running.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.store import Store
from tests.conftest import HEADERS, bearer

PROJECT = "demo-inv-studio-test"
OWNER = "owner@example.test"
CLI = shutil.which("firebase") or next((p for p in [Path.home() / ".local/bin/firebase"] if p.exists()), None)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def emulator(tmp_path_factory):
    if os.getenv("FIREBASE_AUTH_EMULATOR_HOST"):
        yield os.environ["FIREBASE_AUTH_EMULATOR_HOST"]
        return
    if CLI is None:
        pytest.skip("firebase-tools is not installed")
    work = tmp_path_factory.mktemp("auth-emulator")
    port = free_port()
    (work / "firebase.json").write_text(json.dumps({"emulators": {
        "auth": {"host": "127.0.0.1", "port": port}, "hub": {"host": "127.0.0.1", "port": free_port()},
        "logging": {"host": "127.0.0.1", "port": free_port()}, "ui": {"enabled": False}}}))
    env = {k: v for k, v in os.environ.items() if k not in ("GOOGLE_APPLICATION_CREDENTIALS", "FIREBASE_TOKEN")}
    env["NO_UPDATE_NOTIFIER"] = "1"
    process = subprocess.Popen([str(CLI), "emulators:start", "--only", "auth", "--project", PROJECT], cwd=work, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    host = f"127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                with urllib.request.urlopen(f"http://{host}/", timeout=1) as response:
                    if json.load(response).get("authEmulator", {}).get("ready"):
                        break
            except (OSError, ValueError):
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the Auth emulator did not start")
            time.sleep(0.5)
        yield host
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)


class Browser:
    """The Identity Toolkit calls the Firebase JS SDK makes for the page and the sign-in gate."""

    def __init__(self, host, tenant_id=None):
        self.base = f"http://{host}/identitytoolkit.googleapis.com/v1"
        self.tenant_id = tenant_id

    def call(self, method, body):
        if self.tenant_id:
            body = {**body, "tenantId": self.tenant_id}
        request = urllib.request.Request(f"{self.base}/{method}?key=fake-api-key", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ProviderRefused(json.load(error).get("error", {}).get("message", "")) from None

    def verify_code(self, code):          # verifyPasswordResetCode
        return self.call("accounts:resetPassword", {"oobCode": code})["email"]

    def confirm_code(self, code, password):  # confirmPasswordReset
        self.call("accounts:resetPassword", {"oobCode": code, "newPassword": password})

    def sign_in(self, email, password):
        return self.call("accounts:signInWithPassword", {"email": email, "password": password, "returnSecureToken": True})["idToken"]


class ProviderRefused(Exception):
    pass


def refused(call, *args):
    with pytest.raises(ProviderRefused) as error:
        call(*args)
    return str(error.value)


@pytest.fixture
def workspace(emulator, tmp_path, monkeypatch):
    """The app as Cloud Run runs it, pointed at the emulator, with a fresh user pool per test."""
    for name in ("GOOGLE_APPLICATION_CREDENTIALS", "K_SERVICE", "INV_STUDIO_AUTH_TENANT_ID", "INV_STUDIO_ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", emulator)
    monkeypatch.setenv("INV_STUDIO_TEST_AUTH_EMULATOR", "1")
    monkeypatch.setenv("INV_STUDIO_CLOUD", "1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("INV_STUDIO_ALLOWED_EMAILS", OWNER)
    monkeypatch.setenv("INV_STUDIO_PUBLIC_URL", "https://studio.example.test")
    urllib.request.urlopen(urllib.request.Request(f"http://{emulator}/emulator/v1/projects/{PROJECT}/accounts", method="DELETE"), timeout=10)

    def start(tenant_id=None):
        from app.authentication import CloudIdentity
        from app.main import create_app
        if tenant_id:
            monkeypatch.setenv("INV_STUDIO_AUTH_TENANT_ID", tenant_id)
        identity = CloudIdentity()
        assert identity.project.startswith("demo-") and identity.admin is not None
        app = create_app(tmp_path / ("data-" + (tenant_id or "pool")), identity=identity, store_type=Store)
        return app, identity, Browser(emulator, tenant_id)
    return start


def code_of(issued):
    assert issued["link"].startswith("https://studio.example.test/set-password#oobCode="), "link shape"
    return issued["link"].split("#oobCode=", 1)[1]


def test_invite_password_sign_in_disable_enable_reset_role_delete(workspace):
    app, identity, browser = workspace()
    identity.client.create_user(email=OWNER, password="owner password 123", email_verified=True)
    with TestClient(app) as client:
        owner = browser.sign_in(OWNER, "owner password 123")
        assert client.get("/api/session", headers=bearer(owner)).json()["role"] == "admin"

        # Invite: the administrator gets a create-password link; no password exists yet.
        response = client.post("/api/users", json={"email": "Ana@Example.test", "name": "Ana", "role": "member"}, headers=bearer(owner))
        assert response.status_code == 201, response.text
        issued = response.json()
        uid, code = issued["user"]["uid"], code_of(issued)
        assert issued["kind"] == "invite" and identity.client.get_user(uid).email == "ana@example.test"
        assert "INVALID_PASSWORD" in refused(browser.sign_in, "ana@example.test", "anything at all 1")

        # The set-password page: verify the code, save the password; the code then stops working.
        assert browser.verify_code(code) == "ana@example.test"
        browser.confirm_code(code, "correct horse battery staple")
        assert "OOB_CODE" in refused(browser.confirm_code, code, "a second password 123")
        member = browser.sign_in("ana@example.test", "correct horse battery staple")
        session = client.get("/api/session", headers=bearer(member)).json()
        assert session["role"] == "member" and session["status"] == "active"
        assert client.get("/api/users", headers=bearer(member)).status_code == 403

        # Disable: refused by the app at once, the provider refuses new sign-ins, and sessions are revoked.
        assert client.post(f"/api/users/{uid}/disable", json={}, headers=bearer(owner)).json()["user"]["status"] == "disabled"
        assert client.get("/api/session", headers=bearer(member)).status_code == 403
        assert client.post("/api/fine-rules/feedback", json={}, headers=bearer(member)).status_code == 401
        assert "USER_DISABLED" in refused(browser.sign_in, "ana@example.test", "correct horse battery staple")
        assert identity.client.get_user(uid).disabled is True

        # Enable: signs in again.
        assert client.post(f"/api/users/{uid}/enable", json={}, headers=bearer(owner)).json()["user"]["status"] == "active"
        member = browser.sign_in("ana@example.test", "correct horse battery staple")
        assert client.get("/api/session", headers=bearer(member)).status_code == 200

        # Forgot password: neutral, flags the row; the administrator issues a reset link; the old password stops working.
        forgot = client.post("/api/password/forgot", json={"email": "ana@example.test"}, headers=HEADERS)
        assert forgot.status_code == 200
        listed = {u["uid"]: u for u in client.get("/api/users", headers=bearer(owner)).json()["users"]}
        assert listed[uid]["reset_requested_at"]
        reset = client.post(f"/api/users/{uid}/password-link", json={}, headers=bearer(owner)).json()
        assert reset["kind"] == "reset"
        browser.confirm_code(code_of(reset), "a brand new long password")
        assert "INVALID_PASSWORD" in refused(browser.sign_in, "ana@example.test", "correct horse battery staple")
        member = browser.sign_in("ana@example.test", "a brand new long password")
        listed = {u["uid"]: u for u in client.get("/api/users", headers=bearer(owner)).json()["users"]}
        assert listed[uid]["reset_requested_at"] is None

        # Role change takes effect on the next request.
        assert client.post(f"/api/users/{uid}/role", json={"role": "admin"}, headers=bearer(owner)).json()["user"]["role"] == "admin"
        assert client.get("/api/users", headers=bearer(member)).status_code == 200
        assert client.post(f"/api/users/{uid}/role", json={"role": "member"}, headers=bearer(owner)).status_code == 200

        # Delete: the sign-in is gone and the old token no longer opens the workspace.
        assert client.delete(f"/api/users/{uid}", headers=bearer(owner)).status_code == 200
        assert "EMAIL_NOT_FOUND" in refused(browser.sign_in, "ana@example.test", "a brand new long password")
        assert client.get("/api/session", headers=bearer(member)).status_code in (401, 403)
        assert client.post("/api/fine-rules/feedback", json={}, headers=bearer(member)).status_code == 401


def test_a_sign_in_of_another_app_in_the_pool_is_refused_and_left_alone(workspace):
    app, identity, browser = workspace()
    identity.client.create_user(email=OWNER, password="owner password 123", email_verified=True)
    other = identity.client.create_user(email="pi-user@example.test", password="pi password 12345", email_verified=True)
    before = identity.client.get_user(other.uid)
    with TestClient(app) as client:
        owner = browser.sign_in(OWNER, "owner password 123")
        response = client.post("/api/users", json={"email": "pi-user@example.test", "name": "", "role": "member"}, headers=bearer(owner))
        assert response.status_code == 409 and "did not create" in response.json()["detail"]
        # Not adopted, not changed, still signs in with its own password, and still has no workspace access.
        after = identity.client.get_user(other.uid)
        assert (after.disabled, after.tokens_valid_after_timestamp, after.email_verified) == (before.disabled, before.tokens_valid_after_timestamp, before.email_verified)
        token = browser.sign_in("pi-user@example.test", "pi password 12345")
        assert client.get("/api/session", headers=bearer(token)).status_code == 403


def create_tenant(host):
    request = urllib.request.Request(f"http://{host}/identitytoolkit.googleapis.com/v2/projects/{PROJECT}/tenants",
                                     data=json.dumps({"displayName": "inv-studio", "allowPasswordSignup": True}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer owner"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["name"].rsplit("/", 1)[1]


def test_tenant_mode_refuses_pool_tokens_and_keeps_users_in_the_tenant(workspace, emulator):
    app, identity, browser = workspace()
    tenant_id = create_tenant(emulator)
    identity.client.create_user(email=OWNER, password="pool password 123", email_verified=True)
    pool_token = browser.sign_in(OWNER, "pool password 123")

    app, identity, browser = workspace(tenant_id)
    identity.client.create_user(email=OWNER, password="tenant password 123", email_verified=True)
    with TestClient(app) as client:
        # The same email signed in to the project pool (as for Product Intelligence) does not open the tenant workspace.
        assert client.get("/api/session", headers=bearer(pool_token)).status_code == 401
        owner = browser.sign_in(OWNER, "tenant password 123")
        assert client.get("/api/session", headers=bearer(owner)).json()["role"] == "admin"

        issued = client.post("/api/users", json={"email": "ten@example.test", "name": "", "role": "member"}, headers=bearer(owner)).json()
        browser.confirm_code(code_of(issued), "tenant member password")
        member = browser.sign_in("ten@example.test", "tenant member password")
        assert client.get("/api/session", headers=bearer(member)).json()["status"] == "active"
        assert identity.client.get_user(issued["user"]["uid"]).tenant_id == tenant_id
        assert "EMAIL_NOT_FOUND" in refused(Browser(emulator).sign_in, "ten@example.test", "tenant member password")
