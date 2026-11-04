"""Re-match open invoices after the item master changes, and record what changed per invoice.

Activation and rollback (app/master_catalog) start a refresh; POST /api/item-master/refresh starts one by hand, for
a catalog loaded by other tooling. It runs in the invoice worker pool, never in the request. Each open invoice goes
through the same rules re-run a review uses (main.ensure_rules); exported invoices are never re-run or changed.
Only the latest refresh is kept: a newer one supersedes a running one, which then stops.
"""
import logging
import uuid
from datetime import datetime, timezone

from .authorization import ADMIN, access

KEY = "item_master_refresh"
WAITING = ("queued", "processing", "error")
log = logging.getLogger("inv_studio.master_refresh")


def line_items(view):
    """Line number -> matched Item Master item (None when the line has no match)."""
    return {l["line"]: l["cells"]["Item"]["value"] for l in (view or {}).get("lines") or []}


def line_diff(before, after):
    old, new = line_items(before), line_items(after)
    diff = {"newly_matched": [], "lost": [], "changed": []}
    for n in sorted(set(old) | set(new)):
        a, b = old.get(n), new.get(n)
        if a == b:
            continue
        if not a:
            diff["newly_matched"].append({"line": n, "item": b})
        elif not b:
            diff["lost"].append({"line": n, "item": a})
        else:
            diff["changed"].append({"line": n, "from": a, "to": b})
    return diff


def now():
    return datetime.now(timezone.utc).isoformat()


def register_master_refresh(app, store, rematch, pool):
    """``rematch(job_id)`` re-runs one invoice's rules (main.ensure_rules); ``pool`` is the invoice worker pool."""

    def current(rid):
        return (store.get(KEY, None) or {}).get("id") == rid

    def save(state):
        with store.connection(True) as c:
            if (store.get(KEY, None, c) or {}).get("id") != state["id"]:
                return False
            store.set(KEY, state, c)
            return True

    def run(state):
        state.update(status="running", started_at=now())
        if not save(state):
            return
        counts = state["counts"]
        try:
            with store.connection() as c:
                ids = [r[0] for r in c.execute("SELECT id FROM jobs ORDER BY id")]
            for jid in ids:
                if not current(state["id"]):
                    return  # superseded by a newer refresh
                job = store.job(jid)
                if not job:
                    continue
                if job.get("export_id"):
                    counts["exported_skipped"] += 1
                    continue
                if job.get("status") in WAITING:
                    counts["not_ready_skipped"] += 1
                    continue
                try:
                    rematch(jid)
                except Exception as error:
                    counts["failed"] += 1
                    store.audit("item_master_refresh_failed", {"job_id": jid, "error_type": type(error).__name__})
                    continue
                counts["rematched"] += 1
                diff = line_diff(job.get("rules"), (store.job(jid) or {}).get("rules"))
                if any(diff.values()):
                    state["invoices"].append({"job_id": jid, "filename": job.get("filename"),
                                              "number": (job.get("invoice") or {}).get("number"), "lines": diff})
            state.update(status="done", finished_at=now())
        except Exception as error:
            log.exception("Item master refresh failed")
            state.update(status="failed", error=type(error).__name__, finished_at=now())
        if save(state):
            store.audit("item_master_refreshed", {"refresh_id": state["id"], "reason": state["reason"],
                                                  "status": state["status"], "counts": counts,
                                                  "invoices_changed": len(state["invoices"])})

    def start(reason, version=None):
        """Queue a refresh after the live catalog changed; call it after that change has committed."""
        state = {"id": uuid.uuid4().hex, "reason": reason, "version": version, "status": "queued", "queued_at": now(),
                 "counts": {"rematched": 0, "exported_skipped": 0, "not_ready_skipped": 0, "failed": 0},
                 "invoices": []}
        store.set(KEY, state)
        pool.submit(run, dict(state, counts=dict(state["counts"]), invoices=[]))
        return {k: state[k] for k in ("id", "reason", "status")}

    app.state.item_master_refresh = start

    @app.post("/api/item-master/refresh", status_code=202)
    @access(ADMIN)
    def refresh():
        return start("manual")

    @app.get("/api/item-master/refresh")
    @access(ADMIN)
    def latest():
        return store.get(KEY, None)
