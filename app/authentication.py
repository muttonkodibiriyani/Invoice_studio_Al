"""Firebase identity verification for the explicitly enabled cloud workspace.

Verification answers "who is this?" only. Whether that person may use the workspace, and as what, is decided by
the user directory and the route access levels (see app/authorization.py and app/users.py).
"""
import os
from contextvars import ContextVar

actor = ContextVar("invoice_actor", default="local-operator")
LOOKUP_TIMEOUT = 5


def verify_only_credential():
    """A Firebase credential that holds no key. ID tokens are checked against Google's public certificates, so
    verifying needs none, and this never falls back to Application Default Credentials."""
    from firebase_admin import credentials
    from google.auth.credentials import AnonymousCredentials

    class VerifyOnly(credentials.Base):
        def get_credential(self):
            return AnonymousCredentials()
    return VerifyOnly()


class CloudIdentity:
    def __init__(self):
        self.cloud = os.getenv("INV_STUDIO_CLOUD") == "1"
        self.project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
        # Bootstrap lists: these addresses get an admin (or member) row on first sign-in; an address on both is an
        # admin. Everyone else is created by an administrator inside the app.
        self.bootstrap_admins = {x.strip().casefold() for x in os.getenv("INV_STUDIO_ALLOWED_EMAILS", "").split(",") if x.strip()}
        self.bootstrap_members = {x.strip().casefold() for x in os.getenv("INV_STUDIO_MEMBER_EMAILS", "").split(",") if x.strip()}
        self.tenant_id = os.getenv("INV_STUDIO_AUTH_TENANT_ID", "").strip()
        self.web_api_key = os.getenv("INV_STUDIO_FIREBASE_API_KEY", "")
        self.public_url = os.getenv("INV_STUDIO_PUBLIC_URL", "").strip() or next(
            (x.strip() for x in os.getenv("INV_STUDIO_ALLOWED_ORIGINS", "").split(",") if x.strip()), "")
        self.app = None
        self.admin = None
        self.client = None
        if self.cloud:
            if not self.project:
                raise ValueError("Cloud project must be configured")
            # The Auth emulator accepts unsigned tokens. Only the test suite may use it, by also setting a flag that no
            # image or deployment template sets, so a copied setting can never reach Cloud Run or Azure.
            if os.getenv("FIREBASE_AUTH_EMULATOR_HOST") and os.getenv("INV_STUDIO_TEST_AUTH_EMULATOR") != "1":
                raise RuntimeError("FIREBASE_AUTH_EMULATOR_HOST is for local tests only")
            # INV_STUDIO_IDENTITY_ADMIN=0: this deployment holds no Google credential for sign-in (Azure). A key for a
            # sign-in pool shared with other apps could sign in as any of their users. It verifies tokens only, checks
            # revocation through the browser's own lookup call, and users are managed on the primary workspace.
            setting = os.getenv("INV_STUDIO_IDENTITY_ADMIN", "1")
            if setting not in ("0", "1"):
                raise ValueError("INV_STUDIO_IDENTITY_ADMIN must be 0 or 1")
            keyless = setting == "0"
            if keyless and not self.web_api_key:
                raise ValueError("INV_STUDIO_FIREBASE_API_KEY must be configured when INV_STUDIO_IDENTITY_ADMIN=0")
            import firebase_admin
            from firebase_admin import auth
            from .user_admin import FirebaseUserAdmin
            self.app = firebase_admin.initialize_app(verify_only_credential() if keyless else None,
                                                     options={"projectId": self.project}, name="invoice-studio-"+os.urandom(6).hex())
            # Tokens are verified by the same client that manages users: the project's pool, or the workspace's own
            # Identity Platform tenant when INV_STUDIO_AUTH_TENANT_ID is set (a project-pool token is refused there).
            self.client = auth.Client(self.app, tenant_id=self.tenant_id or None)
            self.admin = None if keyless else FirebaseUserAdmin(self.client)

    def _check_account(self, token, claims):
        """Without an Admin credential, revocation and disabled accounts are checked the way the browser looks up its
        own account: Identity Toolkit accounts:lookup with the public web API key and the caller's ID token. The rule
        is firebase-admin's (refused when disabled, or when the token was issued before validSince). Any error,
        timeout or unexpected answer refuses the request; nothing is cached."""
        import httpx
        emulator = os.getenv("FIREBASE_AUTH_EMULATOR_HOST")
        base = f"http://{emulator}/identitytoolkit.googleapis.com" if emulator else "https://identitytoolkit.googleapis.com"
        headers = {"X-Goog-Api-Key": self.web_api_key}  # a header, so the key is never part of a logged URL
        if self.public_url:
            headers["Referer"] = self.public_url.rstrip("/") + "/"
        body = {"idToken": token, **({"tenantId": self.tenant_id} if self.tenant_id else {})}
        response = httpx.post(base + "/v1/accounts:lookup", json=body, headers=headers, timeout=LOOKUP_TIMEOUT)
        response.raise_for_status()
        users = response.json().get("users") or []
        if len(users) != 1 or users[0].get("localId") != claims["uid"]:
            raise PermissionError("account lookup did not return this user")
        if users[0].get("disabled") is True:
            raise PermissionError("account disabled")
        if int(claims["iat"]) < int(users[0].get("validSince", 0)):
            raise PermissionError("token revoked")

    def config(self):
        if not self.cloud:
            return {"cloud": False}
        firebase = {
            "apiKey": os.getenv("INV_STUDIO_FIREBASE_API_KEY", ""),
            "authDomain": os.getenv("INV_STUDIO_FIREBASE_AUTH_DOMAIN", ""),
            "projectId": self.project,
            "appId": os.getenv("INV_STUDIO_FIREBASE_APP_ID", ""),
            "providers": ["password"],
        }
        if self.tenant_id:
            firebase["tenantId"] = self.tenant_id
        return {"cloud": True, "firebase": firebase}

    def verify(self, authorization, check_revoked=False):
        """The verified identity behind a bearer token: {"uid", "email", "email_verified"}.

        ValueError means "not signed in" (401); the caller decides authorization (403).
        """
        if not authorization or not authorization.startswith("Bearer "):
            raise ValueError("Sign in to Invoice Studio")
        token = authorization[7:]
        if len(token) > 16384:
            raise ValueError("Invalid sign-in token")
        try:
            claims = self.client.verify_id_token(token, check_revoked=check_revoked and self.admin is not None)
            if check_revoked and self.admin is None:
                self._check_account(token, claims)
        except Exception:
            raise ValueError("Sign-in expired or could not be verified. Sign in again.") from None
        if claims.get("firebase", {}).get("tenant") != (self.tenant_id or None):
            raise ValueError("Sign-in expired or could not be verified. Sign in again.")
        email = str(claims.get("email", "")).casefold()
        if not email:
            raise PermissionError("This account has no email address")
        return {"email": email, "uid": claims["uid"], "email_verified": claims.get("email_verified") is True}
