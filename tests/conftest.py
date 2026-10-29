"""Shared fixtures: the app in cloud mode with a stand-in Entra tenant and the SQLite store.

No network and no PostgreSQL. `Tenant` signs v2 access tokens with a test RSA key, and the real EntraIdentity checks
them against its JWKS, handed in through the `fetch` seam instead of being downloaded.
"""
import json
import os
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("INV_STUDIO_LEARN", "0")
os.environ.pop("INV_STUDIO_CLOUD", None)
os.environ.pop("INV_STUDIO_DEMO_REFERENCES", None)

from app.authentication import EntraIdentity  # noqa: E402
from app.main import create_app  # noqa: E402
from app.store import Store  # noqa: E402

OWNER = "owner@example.test"
HEADERS = {"X-Studio-Request": "1"}
# Synthetic GUIDs for tests only, not a real tenant or app registration.
TENANT_ID = "11111111-2222-4333-8444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Tenant:
    """The Entra tenant as the app sees it: its signing keys (JWKS) and the access tokens it issues."""

    def __init__(self):
        self.kid = "test-kid"
        self.fetched = []  # the URL of every JWKS fetch

    def jwks(self, url):
        self.fetched.append(url)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(SIGNING_KEY.public_key()))
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def token(self, oid, email="", roles=("member",), *, key=SIGNING_KEY, algorithm="RS256", headers=None, **claims):
        """A v2 access token for our API from our SPA. A claim passed as None is left out."""
        now = int(time.time())
        body = {"iss": ISSUER, "aud": CLIENT_ID, "tid": TENANT_ID, "iat": now, "nbf": now, "exp": now + 3600,
                "ver": "2.0", "scp": "access_as_user", "azp": CLIENT_ID, "oid": oid,
                "preferred_username": email, "name": email.split("@")[0], "roles": None if roles is None else list(roles)}
        body = {k: v for k, v in {**body, **claims}.items() if v is not None}
        return jwt.encode(body, key, algorithm=algorithm, headers={"kid": self.kid, **(headers or {})})


def entra_env(monkeypatch):
    monkeypatch.setenv("INV_STUDIO_CLOUD", "1")
    monkeypatch.setenv("INV_STUDIO_ENTRA_TENANT_ID", TENANT_ID)
    monkeypatch.setenv("INV_STUDIO_ENTRA_CLIENT_ID", CLIENT_ID)


@pytest.fixture
def tenant():
    return Tenant()


@pytest.fixture
def identity(tenant, monkeypatch):
    entra_env(monkeypatch)
    return EntraIdentity(fetch=tenant.jwks)


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
def owner(tenant, client):
    """An administrator (the token carries the admin app role), signed in once so their entra_users row exists."""
    token = tenant.token("owner-uid", OWNER, roles=("admin",))
    response = client.get("/api/session", headers=bearer(token))
    assert response.status_code == 200, response.text
    return token


def audit_rows(app):
    with app.state.store.connection() as c:
        return [(r["event"], r["payload"]) for r in c.execute("SELECT event,payload FROM audit ORDER BY id")]
