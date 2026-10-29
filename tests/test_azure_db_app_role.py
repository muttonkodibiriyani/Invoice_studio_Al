"""scripts/azure_db_app_role.py against a local PostgreSQL set up the way Azure Flexible Server is.

On Azure the admin login is not a superuser: it has CREATEROLE and CREATEDB and is a member of
azure_pg_admin, which owns the database. The test builds exactly that, serves TLS with a throwaway CA
and a certificate for a *.postgres.database.azure.com name, starts the app's schema as the admin, runs
`create`, and restarts the schema as the new role over sslmode=verify-full.
"""
import datetime
import getpass
import importlib.util
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

psycopg = pytest.importorskip("psycopg")
from cryptography import x509  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HOST = "invstudio-db-test.postgres.database.azure.com"


def load_script():
    spec = importlib.util.spec_from_file_location("azure_db_app_role", ROOT / "scripts" / "azure_db_app_role.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_tls(directory):
    now = datetime.datetime.now(datetime.timezone.utc)
    window = dict(not_valid_before=now - datetime.timedelta(minutes=5), not_valid_after=now + datetime.timedelta(days=1))
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root")])
    ca = (x509.CertificateBuilder(subject_name=ca_name, issuer_name=ca_name, public_key=ca_key.public_key(),
                                  serial_number=x509.random_serial_number(), **window)
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(ca_key, hashes.SHA256()))
    server = (x509.CertificateBuilder(subject_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]),
                                      issuer_name=ca_name, public_key=key.public_key(),
                                      serial_number=x509.random_serial_number(), **window)
              .add_extension(x509.SubjectAlternativeName([x509.DNSName("*.postgres.database.azure.com")]), critical=False)
              .sign(ca_key, hashes.SHA256()))
    (directory / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (directory / "server.crt").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    (directory / "server.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (directory / "server.key").chmod(0o600)


@pytest.fixture
def azure_like_postgres():
    candidates = list(Path("/usr/lib/postgresql").glob("*/bin/initdb"))
    initdb = shutil.which("initdb") or (str(sorted(candidates)[-1]) if candidates else None)
    if not initdb or os.geteuid() == 0:
        pytest.skip("A local non-root PostgreSQL installation is needed")
    pg_ctl = str(Path(initdb).with_name("pg_ctl"))
    with tempfile.TemporaryDirectory(prefix="app-role-pg-") as name:
        directory = Path(name)
        data = directory / "db"
        write_tls(directory)
        subprocess.run([initdb, "-D", str(data), "-A", "trust", "--no-locale", "--encoding=UTF8"],
                       check=True, capture_output=True)
        (data / "pg_hba.conf").write_text("local all all trust\nhostssl all all 127.0.0.1/32 scram-sha-256\n")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        options = (f"-h 127.0.0.1 -k {directory} -p {port} -c ssl=on -c ssl_cert_file={directory}/server.crt "
                   f"-c ssl_key_file={directory}/server.key")
        subprocess.run([pg_ctl, "-D", str(data), "-l", str(directory / "server.log"), "-o", options, "-w", "start"],
                       check=True, capture_output=True)
        try:
            admin_password = Fernet.generate_key().decode()
            with psycopg.connect(host=str(directory), port=port, dbname="postgres", autocommit=True) as c:
                c.execute("CREATE ROLE azure_pg_admin NOLOGIN")
                c.execute(psycopg.sql.SQL("CREATE ROLE invstudio LOGIN CREATEROLE CREATEDB PASSWORD {}").format(
                    psycopg.sql.Literal(admin_password)))
                c.execute("GRANT azure_pg_admin TO invstudio")
                c.execute("CREATE DATABASE invstudio OWNER azure_pg_admin")

            def url(user, password, sslmode="verify-full"):
                return (f"postgresql://{user}:{password}@{HOST}:{port}/invstudio?hostaddr=127.0.0.1"
                        f"&sslmode={sslmode}&sslrootcert={directory}/ca.crt")
            yield SimpleNamespace(admin=url("invstudio", admin_password), url=url)
        finally:
            subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"], check=True, capture_output=True)


def start_app(tmp_path, monkeypatch, database_url):
    """The schema work app/main.py does at start in cloud mode."""
    from fastapi import FastAPI

    from app import mapping_tables
    from app.cloud_store import PostgresStore
    from app.reference_lookup import ReferenceLookup
    from app.users import UserDirectory

    monkeypatch.setenv("INV_STUDIO_DATABASE_URL", database_url)
    monkeypatch.setenv("INV_STUDIO_BUCKET", "synthetic-unused")
    monkeypatch.setenv("INV_STUDIO_VAULT_KEY", Fernet.generate_key().decode())
    store = PostgresStore(Path(tempfile.mkdtemp(dir=tmp_path)), _storage_client=SimpleNamespace(bucket=lambda _: None))
    mapping_tables.register_mapping_tables(FastAPI(), store)
    UserDirectory(store).ensure_schema()
    ReferenceLookup(store)
    return store


def test_the_app_moves_to_a_least_privilege_role(azure_like_postgres, tmp_path, monkeypatch, capsys):
    script = load_script()
    database = azure_like_postgres
    password = Fernet.generate_key().decode()[:24]
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": password)

    as_admin = start_app(tmp_path, monkeypatch, database.admin)
    as_admin.set("written_by", "admin")
    with pytest.raises(SystemExit):
        script.check()  # still the admin
    assert "logged in as invstudio, not invstudio_app" in capsys.readouterr().err

    script.create()
    out = capsys.readouterr().out
    assert "Created role invstudio_app" in out and "settings" in out and "OK: certificate verified" in out
    assert password not in out
    assert as_admin.get("written_by") == "admin", "the running admin revision keeps working until it restarts"

    script.create()  # a second run only re-sets the password
    assert "Updated role invstudio_app" in capsys.readouterr().out

    app_url = database.url("invstudio_app", password)
    as_app = start_app(tmp_path, monkeypatch, app_url)  # every CREATE ... IF NOT EXISTS again, as the owner
    assert as_app.get("written_by") == "admin"
    as_app.set("written_by", "app")
    script.check()
    assert "OK: certificate verified" in capsys.readouterr().out

    with psycopg.connect(app_url, autocommit=True) as connection:
        connection.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS probe TEXT")
        connection.execute("ALTER TABLE settings DROP COLUMN probe")
        connection.execute("CREATE TEMP TABLE stage(x int)")
        for forbidden in ("CREATE ROLE intruder LOGIN", "CREATE DATABASE other", "SELECT * FROM pg_authid",
                          "GRANT azure_pg_admin TO invstudio_app", "SET ROLE invstudio"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(forbidden)

    monkeypatch.setenv("INV_STUDIO_DATABASE_URL", database.url("invstudio_app", password, "require"))
    with pytest.raises(SystemExit):
        script.check()
    assert "sslmode is require, not verify-full" in capsys.readouterr().err


def test_verify_full_refuses_a_name_the_certificate_does_not_carry(azure_like_postgres):
    # The private-link name is not in the server certificate; the app must keep using the usual name.
    wrong = azure_like_postgres.admin.replace(HOST, "invstudio-db-test.privatelink.postgres.database.azure.com")
    with pytest.raises(psycopg.OperationalError, match="does not match host name"):
        psycopg.connect(wrong, connect_timeout=5)
