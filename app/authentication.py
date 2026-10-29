"""Entra ID sign-in for the cloud workspace (design doc "Entra ID sign-in design" §3).

The API accepts only v2 access tokens that the team's own tenant issued to this app's SPA for this API. Entra decides
who gets in and with which role: the role is read from the token's `roles` claim on every request, and nothing the
app stores can grant one. Route access levels are in app/authorization.py. The backend holds no credential: tokens
are checked against the tenant's public signing keys.
"""
import logging
import os
import re
import threading
import time
from contextvars import ContextVar

import jwt

actor = ContextVar("invoice_actor", default="local-operator")
AUTHORITY = "https://login.microsoftonline.com"
SCOPE = "access_as_user"
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Settings of the Firebase build. One left in a cloud deployment would look as if it works, so it stops the start.
LEFTOVERS = ("GOOGLE_CLOUD_PROJECT", "INV_STUDIO_ALLOWED_EMAILS", "INV_STUDIO_MEMBER_EMAILS")
EXPIRED = "Sign-in expired or could not be verified. Sign in again."
NO_ROLE = "This account has not been granted access to this workspace"
KEYS_TTL = 86400  # seconds the tenant's keys are kept
REFETCH_GAP = 300  # an unknown kid refetches the keys at most this often
LEEWAY = 60
FETCH_TIMEOUT = 5
log = logging.getLogger("inv_studio.auth")


def fetch_json(url):
    import httpx
    response = httpx.get(url, timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    return response.json()


class TenantKeys:
    """The tenant's token-signing keys (its JWKS), cached in process. If they cannot be fetched and none are cached,
    no token verifies: sign-in fails closed."""

    def __init__(self, tenant_id, fetch):
        self.url = f"{AUTHORITY}/{tenant_id}/discovery/v2.0/keys"
        self.fetch = fetch
        self.keys = {}
        self.loaded = None  # monotonic time of the last successful fetch
        self.tried = None  # monotonic time of the last attempt
        self.lock = threading.Lock()

    def get(self, kid):
        with self.lock:
            now = time.monotonic()
            stale = self.loaded is None or now - self.loaded > KEYS_TTL
            due = self.tried is None or now - self.tried >= REFETCH_GAP
            if due and (stale or kid not in self.keys):
                self.tried = now
                try:
                    self.keys = {k["kid"]: jwt.PyJWK(k, "RS256").key for k in self.fetch(self.url)["keys"]
                                 if k.get("kty") == "RSA" and k.get("use", "sig") == "sig" and k.get("kid")}
                    self.loaded = now
                except Exception:
                    log.exception("could not fetch the tenant's signing keys")
            return self.keys.get(kid)


class EntraIdentity:
    def __init__(self, fetch=fetch_json):
        """`fetch` is the test seam: tests hand in the JWKS of a test key instead of downloading the tenant's."""
        self.cloud = os.getenv("INV_STUDIO_CLOUD") == "1"
        # Lower case, as Entra writes them in iss, tid, aud and azp, which are compared exactly.
        self.tenant_id = os.getenv("INV_STUDIO_ENTRA_TENANT_ID", "").strip().lower()
        self.client_id = os.getenv("INV_STUDIO_ENTRA_CLIENT_ID", "").strip().lower()
        # Display only, for the Users screen: who to ask for access. They decide nothing.
        self.groups = {"admin": os.getenv("INV_STUDIO_ENTRA_ADMIN_GROUP_NAME", "").strip(),
                       "member": os.getenv("INV_STUDIO_ENTRA_MEMBER_GROUP_NAME", "").strip()}
        self.issuer = f"{AUTHORITY}/{self.tenant_id}/v2.0"
        if self.cloud:
            # A GUID, so never common/organizations/consumers: a multi-tenant authority would accept any tenant.
            for name, value in (("INV_STUDIO_ENTRA_TENANT_ID", self.tenant_id), ("INV_STUDIO_ENTRA_CLIENT_ID", self.client_id)):
                if not GUID.match(value):
                    raise ValueError(name + " must be the GUID of the workforce tenant and its app registration")
            for name in sorted(os.environ):
                if name in LEFTOVERS or name.startswith("INV_STUDIO_FIREBASE_"):
                    raise ValueError(name + " is a Firebase setting; this build signs in with Entra ID only. Remove it.")
        self.keys = TenantKeys(self.tenant_id, fetch)

    def config(self):
        if not self.cloud:
            return {"cloud": False}
        return {"cloud": True, "entra": {"clientId": self.client_id, "tenantId": self.tenant_id,
                                         "authority": f"{AUTHORITY}/{self.tenant_id}",
                                         "scopes": [f"api://{self.client_id}/{SCOPE}"]}}

    def verify(self, authorization):
        """The signed-in user behind a bearer token: {"uid" (the oid), "email", "name", "role"}.

        ValueError means "not signed in" (401) and says nothing about why; PermissionError means signed in but given
        no app role (403).
        """
        if not authorization or not authorization.startswith("Bearer "):
            raise ValueError("Sign in to Invoice Studio")
        token = authorization[7:]
        if len(token) > 16384:
            raise ValueError(EXPIRED)
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError("algorithm or key id")
            key = self.keys.get(header["kid"])
            if key is None:
                raise ValueError("unknown signing key")
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.client_id, issuer=self.issuer,
                                leeway=LEEWAY, options={"require": ["exp", "nbf", "iat", "iss", "aud"]})
            if claims["aud"] != self.client_id or claims.get("tid") != self.tenant_id or claims.get("ver") != "2.0":
                raise ValueError("audience, tenant or version")
            if SCOPE not in str(claims.get("scp", "")).split() or claims.get("azp") != self.client_id:
                raise ValueError("not a delegated token from this app")
            if not isinstance(claims.get("oid"), str) or not claims["oid"]:
                raise ValueError("no object id")
        except Exception as exc:
            # Which check refused it, for the operator (a v1 token or a missing scope after a registration mistake).
            # Only our own short reasons or PyJWT's error class: never the token or a claim value.
            log.info("sign-in token refused: %s", str(exc) if type(exc) is ValueError else type(exc).__name__)
            raise ValueError(EXPIRED) from None
        roles = claims.get("roles") if isinstance(claims.get("roles"), list) else []
        role = "admin" if "admin" in roles else "member" if "member" in roles else None
        if role is None:
            raise PermissionError(NO_ROLE)
        email = str(claims.get("preferred_username") or claims.get("email") or "").casefold()
        return {"uid": claims["oid"], "email": email, "name": str(claims.get("name") or "")[:120], "role": role}
