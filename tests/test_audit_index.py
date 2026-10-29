"""One invoice's audit history is read through an index, including records an older version wrote."""
import json
import sqlite3

from app.store import Store


def test_an_older_audit_table_gains_an_indexed_invoice_column(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    with sqlite3.connect(root / "studio.sqlite3") as c:
        c.execute("CREATE TABLE audit(id INTEGER PRIMARY KEY, event TEXT NOT NULL, payload TEXT NOT NULL, "
                  "at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        c.executemany("INSERT INTO audit(event,payload) VALUES (?,?)",
                      [("uploaded", json.dumps({"job_id": "a"})), ("settings", json.dumps({})),
                       ("uploaded", json.dumps({"job_id": "b"}))])
    store = Store(root)
    store.audit("reviewed", {"job_id": "a"})
    with store.connection() as c:
        assert [r[0] for r in c.execute("SELECT event FROM audit WHERE job_id=? ORDER BY id", ("a",))] == ["uploaded", "reviewed"]
        assert [r[0] for r in c.execute("SELECT job_id FROM audit ORDER BY id")] == ["a", "", "b", "a"]
        plan = " ".join(r[-1] for r in c.execute("EXPLAIN QUERY PLAN SELECT event,at,payload FROM audit WHERE job_id=? ORDER BY id", ("a",)))
    assert "USING INDEX audit_job" in plan
