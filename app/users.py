"""The workspace user directory (`app_users`).

Firebase remains the identity provider; this table decides who may use the workspace and as what. Rows are
soft-deleted (`deleted_at`). `org_id` keeps the data model open for several companies later; v1 is one company, so
every row is in the default organisation.

`login_origin` records who owns the sign-in behind a row. The Firebase project's user pool is shared with Product
Intelligence, so the app only ever changes sign-ins it created itself ('app'): it disables, re-enables, issues password
links for and deletes those. A sign-in that existed before ('existing', for example the bootstrap owner's login, which
Product Intelligence also uses) is only ever allowed or refused inside this app; the provider account is never touched.

The migration is additive: CREATE TABLE/INDEX IF NOT EXISTS only, and the SQL is shared by SQLite and PostgreSQL
(the Postgres store translates `?` placeholders).
"""
from datetime import datetime, timezone

ROLES = ("admin", "member")
STATUSES = ("invited", "active", "disabled")
ORIGINS = ("app", "existing")
DEFAULT_ORG = "default"

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS app_users (
        uid TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL CHECK (role IN ('admin','member')),
        status TEXT NOT NULL CHECK (status IN ('invited','active','disabled')),
        org_id TEXT NOT NULL DEFAULT 'default',
        invited_by TEXT NOT NULL DEFAULT '',
        invited_at TEXT NOT NULL,
        last_login_at TEXT,
        reset_requested_at TEXT,
        deleted_at TEXT,
        login_origin TEXT NOT NULL DEFAULT 'existing' CHECK (login_origin IN ('app','existing')))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS app_users_live_email ON app_users (email) WHERE deleted_at IS NULL",
)

COLUMNS = ("uid", "email", "name", "role", "status", "org_id", "invited_by", "invited_at", "last_login_at", "reset_requested_at",
           "deleted_at", "login_origin")
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM app_users"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def recent(stamp, seconds):
    """Is an ISO timestamp written by now_iso() less than `seconds` old?"""
    if not stamp:
        return False
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() < seconds
    except ValueError:
        return False


def normalize_email(email):
    return str(email or "").strip().casefold()


def _record(row):
    if row is None:
        return None
    return {column: row[column] for column in COLUMNS}


class UserDirectory:
    def __init__(self, store):
        self.store = store

    def ensure_schema(self):
        with self.store.connection(True) as c:
            for statement in SCHEMA:
                c.execute(statement)

    def get(self, uid, c=None):
        if c is None:
            with self.store.connection() as connection:
                return self.get(uid, connection)
        return _record(c.execute(_SELECT + " WHERE uid=? AND deleted_at IS NULL", (uid,)).fetchone())

    def removed(self, uid, c=None):
        """Was this sign-in's row deleted from the workspace? (A uid stays reserved after deletion.)"""
        if c is None:
            with self.store.connection() as connection:
                return self.removed(uid, connection)
        return c.execute("SELECT 1 FROM app_users WHERE uid=? AND deleted_at IS NOT NULL", (uid,)).fetchone() is not None

    def by_email(self, email, c=None):
        if c is None:
            with self.store.connection() as connection:
                return self.by_email(email, connection)
        return _record(c.execute(_SELECT + " WHERE email=? AND deleted_at IS NULL", (normalize_email(email),)).fetchone())

    def list(self, c=None):
        if c is None:
            with self.store.connection() as connection:
                return self.list(connection)
        return [_record(r) for r in c.execute(_SELECT + " WHERE deleted_at IS NULL ORDER BY invited_at, email").fetchall()]

    def create(self, uid, email, name, role, invited_by, status="invited", org_id=DEFAULT_ORG, c=None, *, login_origin):
        if role not in ROLES or status not in STATUSES or login_origin not in ORIGINS:
            raise ValueError("Unknown role, status or login origin")
        if c is None:
            with self.store.connection(True) as connection:
                return self.create(uid, email, name, role, invited_by, status, org_id, connection, login_origin=login_origin)
        c.execute(
            "INSERT INTO app_users (uid,email,name,role,status,org_id,invited_by,invited_at,login_origin) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, normalize_email(email), (name or "").strip()[:120], role, status, org_id, invited_by, now_iso(), login_origin),
        )
        return self.get(uid, c)

    def update(self, uid, c=None, **fields):
        allowed = {"name", "role", "status", "last_login_at", "reset_requested_at", "deleted_at", "org_id"}
        unknown = set(fields) - allowed
        if unknown or not fields:
            raise ValueError("Unknown user fields: " + ", ".join(sorted(unknown)))
        if "role" in fields and fields["role"] not in ROLES:
            raise ValueError("Unknown role")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError("Unknown status")
        if c is None:
            with self.store.connection(True) as connection:
                return self.update(uid, connection, **fields)
        assignments = ", ".join(f"{column}=?" for column in fields)
        c.execute(f"UPDATE app_users SET {assignments} WHERE uid=? AND deleted_at IS NULL", (*fields.values(), uid))
        return self.get(uid, c)

    def soft_delete(self, uid, c=None):
        if c is None:
            with self.store.connection(True) as connection:
                return self.soft_delete(uid, connection)
        c.execute("UPDATE app_users SET deleted_at=? WHERE uid=? AND deleted_at IS NULL", (now_iso(), uid))

    def active_admins(self, c=None):
        if c is None:
            with self.store.connection() as connection:
                return self.active_admins(connection)
        return [_record(r) for r in c.execute(_SELECT + " WHERE role='admin' AND status='active' AND deleted_at IS NULL").fetchall()]
