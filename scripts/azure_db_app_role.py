"""Move the Azure app off the PostgreSQL admin login onto its own role (security review M1).

Run once, inside the app container, while INV_STUDIO_DATABASE_URL still holds the admin login
(docs/AZURE.md §5a):

    python scripts/azure_db_app_role.py create   # asks for the new role's password, never prints it
    python scripts/azure_db_app_role.py check    # after the switch: exits 1 unless the app is least-privilege

`create` makes the LOGIN role `invstudio_app` (no superuser, CREATEDB, CREATEROLE, replication or
azure_pg_admin membership), gives it CONNECT and TEMPORARY on this database and USAGE and CREATE on
schema `public`, and hands it every table, view and sequence the admin owns there. The app runs its own
CREATE TABLE / INDEX / ALTER TABLE at start, so it must own its tables, and needs nothing server-wide.
The password goes to the server only as a SCRAM verifier computed here. Running it again re-sets the
password and hands over anything the admin created since; it changes nothing else.

`check` connects with INV_STUDIO_DATABASE_URL as the app does and fails unless the connection verifies
the server certificate (sslmode=verify-full) and the login is a plain role that owns the app's tables.
"""
import getpass
import os
import sys

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

APP_ROLE = "invstudio_app"
MIN_PASSWORD = 16
# r table, p partitioned table, v view, m materialized view, S sequence. A sequence that belongs to a
# column (BIGSERIAL) moves with its table and cannot be handed over on its own.
OWNED = """SELECT c.relkind, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S')
             AND pg_get_userbyid(c.relowner) = current_user
             AND NOT (c.relkind = 'S' AND EXISTS (
                 SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype IN ('a','i')))
           ORDER BY c.relname"""
KEYWORD = {"r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW", "S": "SEQUENCE"}


def fail(message):
    print(f"FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def database_url():
    url = os.getenv("INV_STUDIO_DATABASE_URL", "").strip()
    if not url:
        fail("INV_STUDIO_DATABASE_URL is not set; run this inside the app container")
    return url


def read_password():
    password = getpass.getpass(f"New password for {APP_ROLE} (input hidden): ")
    if len(password) < MIN_PASSWORD:
        fail(f"use at least {MIN_PASSWORD} characters")
    if getpass.getpass("Again: ") != password:
        fail("the two entries differ")
    return password


def create():
    url = database_url()
    with psycopg.connect(url, connect_timeout=20) as connection:
        admin, database = connection.execute("SELECT current_user, current_database()").fetchone()
        if admin == APP_ROLE:
            fail(f"INV_STUDIO_DATABASE_URL already logs in as {APP_ROLE}; nothing to do")
        password = read_password()
        verifier = connection.pgconn.encrypt_password(password.encode(), APP_ROLE.encode(), b"scram-sha-256")
        role, me = sql.Identifier(APP_ROLE), sql.Identifier(admin)
        exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)).fetchone()
        # ALTER ROLE may not even restate NOSUPERUSER without superuser rights, so a re-run sets only the
        # password; the check at the end refuses a role that has gained any other attribute.
        statement = ("ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else
                     "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                     "INHERIT PASSWORD {}")
        connection.execute(sql.SQL(statement).format(role, sql.Literal(verifier.decode())))
        connection.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
            sql.Identifier(database), role))
        connection.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(role))
        # Handing a table to a role needs SET on it (PostgreSQL 16). INHERIT keeps the admin able to read
        # the app's tables, so the running revision keeps working until it restarts as the new role.
        # This makes the admin a member of the app role, not the other way round.
        connection.execute(sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE").format(role, me))
        moved = []
        for kind, name in connection.execute(OWNED).fetchall():
            connection.execute(sql.SQL("ALTER {} {} OWNER TO {}").format(
                sql.SQL(KEYWORD[kind]), sql.Identifier("public", name), role))
            moved.append(name)
    print(f"{'Updated' if exists else 'Created'} role {APP_ROLE} in database {database}; "
          f"handed over {len(moved)} object(s): {', '.join(moved) or 'none'}")
    verify(url, password)


def verify(url, password):
    """Log in as the new role exactly as the app will, with the TLS settings from the current URL."""
    params = conninfo_to_dict(url)
    params.update(user=APP_ROLE, password=password)
    with psycopg.connect(**params, connect_timeout=20) as connection:
        report(connection)
    print(f"Logged in as {APP_ROLE}. Now put its URL in Key Vault and restart the revision (docs/AZURE.md §5a).")


def report(connection):
    params = conninfo_to_dict(connection.info.dsn)
    problems = []
    if params.get("sslmode") != "verify-full":
        problems.append(f"sslmode is {params.get('sslmode') or 'unset'}, not verify-full")
    if not connection.pgconn.ssl_in_use:
        problems.append("the connection is not encrypted")
    user, superuser, createrole, createdb, admin_member, can_create = connection.execute(
        """SELECT r.rolname, r.rolsuper, r.rolcreaterole, r.rolcreatedb,
                  EXISTS (SELECT 1 FROM pg_roles a WHERE a.rolname = 'azure_pg_admin'
                          AND pg_has_role(r.oid, a.oid, 'USAGE')),
                  has_schema_privilege('public', 'CREATE')
           FROM pg_roles r WHERE r.rolname = current_user"""
    ).fetchone()
    if user != APP_ROLE:
        problems.append(f"logged in as {user}, not {APP_ROLE}")
    for flag, label in ((superuser, "SUPERUSER"), (createrole, "CREATEROLE"), (createdb, "CREATEDB"),
                        (admin_member, "a member of azure_pg_admin")):
        if flag:
            problems.append(f"{user} is {label}")
    if not can_create:
        problems.append(f"{user} cannot create tables in schema public")
    foreign = [name for (name,) in connection.execute(
        """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind IN ('r','p') AND c.relowner <> (
               SELECT oid FROM pg_roles WHERE rolname = current_user)""")]
    if foreign:
        problems.append(f"{user} does not own {', '.join(foreign)}; run `create` again as the admin")
    tls = connection.execute("SELECT version FROM pg_stat_ssl WHERE pid = pg_backend_pid()").fetchone()
    print(f"login {user}, sslmode {params.get('sslmode')}, TLS {tls[0] if tls else 'none'}")
    if problems:
        fail("; ".join(problems))
    print("OK: certificate verified, least-privilege role, owns every app table")


def check():
    with psycopg.connect(database_url(), connect_timeout=20) as connection:
        report(connection)


if __name__ == "__main__":
    commands = {"create": create, "check": check}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        fail("usage: python scripts/azure_db_app_role.py create|check")
    commands[sys.argv[1]]()
