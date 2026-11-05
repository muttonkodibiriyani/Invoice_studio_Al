"""After the item master changes, open invoices are matched again and the change is shown per invoice."""
import time

from app.master_refresh import line_items, line_diff
from app.models import Invoice, Line
from tests.conftest import bearer
from tests.test_latest_master import activate, config


def view(*items):
    return {"lines": [{"line": n, "cells": {"Item": {"value": v}}} for n, v in enumerate(items, 1)]}


def test_line_diff_names_new_lost_and_changed_matches():
    before, after = view(None, "A", "B", "C"), view("X", None, "B2", "C")
    assert line_items(before) == {1: None, 2: "A", 3: "B", 4: "C"}
    assert line_diff(before, after) == {"newly_matched": [{"line": 1, "item": "X"}],
                                        "lost": [{"line": 2, "item": "A"}],
                                        "changed": [{"line": 3, "from": "B", "to": "B2"}]}
    assert line_diff(None, view("X")) == {"newly_matched": [{"line": 1, "item": "X"}], "lost": [], "changed": []}
    assert line_diff(view("A"), view("A")) == {"newly_matched": [], "lost": [], "changed": []}


def seed(store, jid, **fields):
    inv = Invoice(number=jid, date="2026-11-01", supplier_name="Synthetic Co", currency="AED", po="450001",
                  location="1200", net=100, tax=5, lines=[Line(sku="VPN-1", description="Rose red", qty=2, price=50)])
    job = {"id": jid, "filename": jid + ".pdf", "status": "review", "revision": 2, "reviewed": False, "path": "",
           "invoice": inv.model_dump(mode="json"), "evidence": {}, "boxes": [], "trace": [], "provenance": [],
           "text": "Synthetic Co\nInvoice INV-1\nDate 2026-11-01\nDeliver To: 1200\nRose red 2 50 100\nNet 100\nVAT 5",
           "validation": {"ready": False, "issues": [], "matches": []}, **fields}
    store.job(jid, job)
    return job


def finished(client, owner):
    for _ in range(300):
        state = client.get("/api/item-master/refresh", headers=bearer(owner)).json()
        if state and state["status"] in ("done", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError("refresh did not finish")


def test_activation_rematches_open_invoices_and_never_touches_exported(client, owner, app):
    store = app.state.store
    store.set("fine_rules_config", config())
    seed(store, "open-1")
    exported = seed(store, "done-1", status="exported", export_id="exp-1")
    _, result = activate(client, owner)
    assert result["refresh"]["status"] in ("queued", "running", "done")
    state = finished(client, owner)
    assert state["status"] == "done" and state["reason"] == "activated"
    assert state["counts"]["rematched"] == 1 and state["counts"]["exported_skipped"] == 1
    after = store.job("open-1")
    assert after["rules"]["revision"] == after["revision"]
    assert state["invoices"] == [{"job_id": "open-1", "filename": "open-1.pdf", "number": "open-1",
                                  "lines": {"newly_matched": [{"line": 1, "item": "100000001"}], "lost": [], "changed": []}}]
    assert store.job("done-1") == exported


def test_rollback_and_manual_update_rerun_and_report_the_change(client, owner, app):
    store = app.state.store
    store.set("fine_rules_config", config())
    activate(client, owner)
    seed(store, "open-1")
    manual = client.post("/api/item-master/refresh", headers=bearer(owner))
    assert manual.status_code == 202, manual.text
    first = finished(client, owner)
    assert first["reason"] == "manual" and first["counts"]["rematched"] == 1
    assert line_items(store.job("open-1")["rules"]) == {1: "100000001"}
    state = client.get("/api/item-master", headers=bearer(owner)).json()
    snap = next(v for v in state["versions"] if v.get("source") == "snapshot")
    rolled = client.post(f"/api/item-master/versions/{snap['id']}/rollback",
                         params={"expected_signature": state["signature"]}, headers=bearer(owner))
    assert rolled.status_code == 200, rolled.text
    second = finished(client, owner)
    assert second["id"] == rolled.json()["refresh"]["id"] and second["reason"] == "rolled_back"
    # The snapshot is the empty catalog from before the first activation: the match is lost.
    assert second["invoices"][0]["lines"]["lost"] == [{"line": 1, "item": "100000001"}]


def test_refresh_is_admin_only(client, tenant):
    member = tenant.token("member-uid", "member@example.test")
    client.get("/api/session", headers=bearer(member))
    assert client.post("/api/item-master/refresh", headers=bearer(member)).status_code == 403
    assert client.get("/api/item-master/refresh", headers=bearer(member)).status_code == 403
