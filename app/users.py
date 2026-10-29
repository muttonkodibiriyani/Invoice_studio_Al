"""Who has signed in to the cloud workspace (`entra_users`, design doc "Entra ID sign-in design" §4.3).

Entra ID decides who gets in and with which role, from the token on every request (app/authentication.py). This
table only records who has signed in and the role their token carried at their last sign-in, for the read-only Users
screen. Nothing reads it to decide access. Rows are keyed on the Entra object id (`oid`), never on an e-mail address.

The migration is additive: CREATE TABLE IF NOT EXISTS only, and the SQL is shared by SQLite and PostgreSQL (the
Postgres store translates `?` placeholders).
"""
from datetime import datetime, timezone

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS entra_users (
        oid TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        email TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL CHECK (role IN ('admin','member')),
        first_login_at TEXT NOT NULL,
        last_login_at TEXT NOT NULL)""",
)
COLUMNS = ("oid", "tenant_id", "email", "name", "role", "first_login_at", "last_login_at")
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM entra_users"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(row):
    return None if row is None else {column: row[column] for column in COLUMNS}


class UserDirectory:
    def __init__(self, store):
        self.store = store

    def ensure_schema(self):
        with self.store.connection(True) as c:
            for statement in SCHEMA:
                c.execute(statement)

    def get(self, oid, c=None):
        if c is None:
            with self.store.connection() as connection:
                return self.get(oid, connection)
        return _record(c.execute(_SELECT + " WHERE oid=?", (oid,)).fetchone())

    def list(self, c=None):
        if c is None:
            with self.store.connection() as connection:
                return self.list(connection)
        return [_record(r) for r in c.execute(_SELECT + " ORDER BY first_login_at, email").fetchall()]

    def record(self, user, tenant_id, c):
        """Write a sign-in by a verified user ({"uid", "email", "name", "role"}) and return the row as it was before
        (None on a first sign-in)."""
        previous = self.get(user["uid"], c)
        stamp = now_iso()
        if previous is None:
            c.execute("INSERT INTO entra_users (oid,tenant_id,email,name,role,first_login_at,last_login_at) VALUES (?,?,?,?,?,?,?)",
                      (user["uid"], tenant_id, user["email"], user["name"], user["role"], stamp, stamp))
        else:
            c.execute("UPDATE entra_users SET email=?, name=?, role=?, last_login_at=? WHERE oid=?",
                      (user["email"], user["name"], user["role"], stamp, user["uid"]))
        return previous
