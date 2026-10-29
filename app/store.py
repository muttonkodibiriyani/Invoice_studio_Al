import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from cryptography.fernet import Fernet
from .authentication import actor


UNFINISHED="json_extract(payload,'$.status') IN ('queued','processing')"

class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        for name in ("uploads", "exports", "templates", "work"):
            (root / name).mkdir(exist_ok=True, mode=0o700)
        self.path = root / "studio.sqlite3"
        keyfile = root / "vault.key"
        if not keyfile.exists():
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        self.cipher = Fernet(keyfile.read_bytes())
        with self.connection() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS credentials(key TEXT PRIMARY KEY,value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY, invoice_key TEXT UNIQUE NOT NULL,
                job_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, workbook BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, payload TEXT NOT NULL, workbook BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS deleted_invoices(
                job_id TEXT PRIMARY KEY, invoice_key TEXT UNIQUE, payload TEXT NOT NULL,
                deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, event TEXT NOT NULL,
                payload TEXT NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            ''')
        with self.connection(True) as c:
            # One invoice's history is an index lookup. Rows without the column yet (an older database, or a record an
            # older version wrote) are filled in here; '' marks a record that names no invoice.
            if "job_id" not in {row[1] for row in c.execute("PRAGMA table_info(audit)")}:
                c.execute("ALTER TABLE audit ADD COLUMN job_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS audit_job ON audit(job_id,id)")
            c.execute("UPDATE audit SET job_id=COALESCE(json_extract(payload,'$.job_id'),'') WHERE job_id IS NULL")
            # Recovery and the job heartbeat read only the unfinished jobs, through an index (security review M5).
            c.execute(f"CREATE INDEX IF NOT EXISTS jobs_unfinished ON jobs(id) WHERE {UNFINISHED}")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self, transaction=False):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            if transaction:
                c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def get(self, key, default=None, c=None):
        if c is None:
            with self.connection() as conn:
                return self.get(key, default, conn)
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value, c=None):
        if c is None:
            with self.connection(True) as conn:
                return self.set(key, value, conn)
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def secret(self, key, value=None, delete=False, c=None):
        if c is None:
            with self.connection(value is not None or delete) as connection:
                return self.secret(key,value,delete,connection)
        if delete:
            c.execute("DELETE FROM credentials WHERE key=?", (key,))
            return None
        if value is not None:
            c.execute("INSERT OR REPLACE INTO credentials VALUES (?,?)",
                      (key, self.cipher.encrypt(json.dumps(value).encode())))
            return None
        row = c.execute("SELECT value FROM credentials WHERE key=?", (key,)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    def job(self, job_id, value=None, c=None):
        if c is None:
            with self.connection(value is not None) as conn:
                return self.job(job_id, value, conn)
        if value is not None:
            c.execute("INSERT OR REPLACE INTO jobs VALUES (?,?)", (job_id, json.dumps(value)))
            return value
        row = c.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def unfinished_jobs(self,c):
        return [json.loads(r[0]) for r in c.execute(f"SELECT payload FROM jobs WHERE {UNFINISHED}")]

    def jobs(self,c=None):
        if c is None:
            with self.connection() as connection:
                return self.jobs(connection)
        return [json.loads(r[0]) for r in c.execute("SELECT payload FROM jobs ORDER BY rowid DESC LIMIT 200")]

    def audit(self, event, payload, c=None):
        if c is None:
            with self.connection(True) as conn:
                return self.audit(event, payload, conn)
        c.execute("INSERT INTO audit(event,payload,job_id) VALUES (?,?,?)",
                  (event, json.dumps({**payload, "actor": actor.get()}), payload.get("job_id") or ""))

    def ledger(self, c):
        receipts = [json.loads(r[0]) for r in c.execute("SELECT payload FROM exports")]
        receipts.extend(
            json.loads(r[0])
            for r in c.execute(
                "SELECT payload FROM deleted_invoices WHERE invoice_key IS NOT NULL"
            )
        )
        return receipts

    def delete_upload(self, path):
        target = Path(path).resolve()
        uploads = (self.root / "uploads").resolve()
        if not target.is_relative_to(uploads):
            raise ValueError("Invoice uploads must stay inside the uploads directory")
        target.unlink(missing_ok=True)
