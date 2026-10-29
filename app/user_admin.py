"""Identity-provider operations for user management (Firebase Authentication / Identity Platform).

The app never sends email itself. For "create your password" and "reset your password" it asks the Admin SDK for a
password-reset link, keeps only the out-of-band code from it, and builds its own page link. No project-wide
email template or action URL is changed. Codes and links are returned to the calling administrator and never logged.
"""
from urllib.parse import parse_qs, urlparse


class UserAdminError(Exception):
    """A provider-side failure safe to show to an administrator."""


def code_from_link(link):
    query = parse_qs(urlparse(link).query)
    code = (query.get("oobCode") or [""])[0]
    if not code:
        raise UserAdminError("The identity provider did not return a password code")
    return code


def page_link(base_url, code):
    """The app's own set-password page. The code travels in the fragment, so it never reaches a server log."""
    return base_url.rstrip("/") + "/set-password#oobCode=" + code


class FirebaseUserAdmin:
    def __init__(self, client):
        from firebase_admin import auth
        self._auth = auth
        # An auth.Client for the project pool, or for the workspace's own Identity Platform tenant.
        self._client = client

    def _wrap(self, call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except self._auth.EmailAlreadyExistsError:
            raise UserAdminError("An account with this email already exists in the identity provider") from None
        except self._auth.UserNotFoundError:
            raise UserAdminError("The identity provider has no account for this user") from None
        except Exception as exc:  # noqa: BLE001 - provider errors must not leak internals or links
            raise UserAdminError("The identity provider rejected the request: " + type(exc).__name__) from None

    def find_uid(self, email):
        try:
            return self._client.get_user_by_email(email).uid
        except self._auth.UserNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            raise UserAdminError("The identity provider rejected the request: " + type(exc).__name__) from None

    def create(self, email, name):
        """A password account with no password yet: the user sets one through the create-password link."""
        user = self._wrap(self._client.create_user, email=email, display_name=name or None, email_verified=True, disabled=False)
        return user.uid

    def password_code(self, email):
        link = self._wrap(self._client.generate_password_reset_link, email)
        return code_from_link(link)

    def set_disabled(self, uid, disabled):
        self._wrap(self._client.update_user, uid, disabled=bool(disabled))
        if disabled:
            self._wrap(self._client.revoke_refresh_tokens, uid)

    def revoke(self, uid):
        self._wrap(self._client.revoke_refresh_tokens, uid)

    def delete(self, uid):
        try:
            self._client.delete_user(uid)
        except self._auth.UserNotFoundError:
            return False
        except Exception as exc:  # noqa: BLE001
            raise UserAdminError("The identity provider rejected the request: " + type(exc).__name__) from None
        return True
