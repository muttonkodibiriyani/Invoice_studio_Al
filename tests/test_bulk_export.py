"""Owner decision A (5 Nov): bulk export of the selected invoices.

'Export selected' first asks /api/exports/batch/check for the ready set and the held list with reasons; the real
export then holds only the three template sheets, Transaction Number 1..n. Held invoices stay unexported. The draft of
everything is marked DRAFT in its file name and on every sheet. An exported invoice is never exported again.
"""
import io
import json
from pathlib import Path

import openpyxl
import pytest

from tests.conftest import bearer

OWNER_TEMPLATE = json.loads((Path(__file__).parent / "fixtures" / "target_template_headers.json").read_text())


def found(value="1001"):
    return {"value": value, "evidence": [{"kind": "lookup", "rule": "I2.1"}], "flagged": False}


def job(client, owner, app, tmp_path, jid, number, reviewed=True):
    """An invoice whose current rules view has every target cell evidenced and no issue; held while not reviewed."""
    store = app.state.store
    source = tmp_path / f"{jid}.pdf"
    source.write_bytes(b"%PDF-1.4")
    store.job(jid, {"id": jid, "filename": f"{jid}.pdf", "path": str(source), "status": "review", "revision": 1,
                    "reviewed": reviewed, "provenance": [], "invoice": {"number": number, "lines": [{}, {}]}})
    assert client.get(f"/api/jobs/{jid}", headers=bearer(owner)).status_code == 200  # stores a current rules view
    j = store.job(jid)
    def evidenced(cells):
        return {k: cell if cell["value"] and cell["evidence"] else {**cell, **found()} for k, cell in cells.items()}
    lines = [{**line, "cells": evidenced(line["cells"])} for line in j["rules"]["lines"]]
    fields = evidenced(j["rules"]["fields"])
    fields["location_type"]["value"], fields["date"]["value"] = "Store (S)", "2026-11-04"
    fields["number"] = {**fields["number"], **found(number)}
    j["rules"] = {k: v for k, v in j["rules"].items() if k != "receipt_controls"} | {"issues": [], "lines": lines, "fields": fields}
    store.job(jid, {**j, "reviewed": reviewed})
    return jid


def selection(app, *ids):
    return {"jobs": [{"id": jid, "revision": app.state.store.job(jid)["revision"]} for jid in ids]}


def check(client, owner, app, *ids):
    response = client.post("/api/exports/batch/check", headers=bearer(owner), json=selection(app, *ids))
    assert response.status_code == 200, response.text
    return response.json()


def export(client, owner, app, *ids):
    return client.post("/api/exports/batch", headers=bearer(owner), json=selection(app, *ids))


def book(response):
    return openpyxl.load_workbook(io.BytesIO(response.content))


def column(sheet, name):
    n = [c.value for c in sheet[1]].index(name) + 1
    return [sheet.cell(r, n).value for r in range(2, sheet.max_row + 1)]


@pytest.fixture
def three(client, owner, app, tmp_path):
    return (job(client, owner, app, tmp_path, "a", "INV-A"), job(client, owner, app, tmp_path, "b", "INV-B"),
            job(client, owner, app, tmp_path, "c", "INV-C", reviewed=False))


def test_the_check_lists_the_ready_set_and_the_held_ones_with_reasons_and_changes_nothing(client, owner, app, three):
    before = {jid: app.state.store.job(jid)["revision"] for jid in three}
    result = check(client, owner, app, *three)
    assert [r["id"] for r in result["ready"]] == ["a", "b"]
    assert [(h["id"], [r["code"] for r in h["reasons"]]) for h in result["held"]] == [("c", ["REVIEW"])]
    assert all(r["message"] for h in result["held"] for r in h["reasons"])
    assert {jid: app.state.store.job(jid)["revision"] for jid in three} == before
    assert not any(app.state.store.job(jid).get("export_id") for jid in three)


def test_export_selected_writes_the_ready_ones_to_one_workbook_with_only_the_template_sheets(client, owner, app, three):
    ready = [r["id"] for r in check(client, owner, app, *three)["ready"]]
    response = export(client, owner, app, *ready)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 2
    workbook = book(client.get(response.json()["url"], headers=bearer(owner)))
    assert workbook.sheetnames == list(OWNER_TEMPLATE)  # no Checks sheet in the real export
    for name, columns in OWNER_TEMPLATE.items():
        assert [c.value for c in workbook[name][1]] == columns
    assert column(workbook["Header"], "Transaction Number") == [1, 2]
    assert column(workbook["Tax_Breakdown"], "Transaction Number") == [1, 2]
    assert column(workbook["Details"], "Transaction Number") == [1, 1, 2, 2]
    assert column(workbook["Header"], "Document") == ["INV-A", "INV-B"]


def test_held_invoices_stay_unexported_and_available_for_a_later_batch(client, owner, app, three):
    assert export(client, owner, app, "a", "b").status_code == 200
    held = app.state.store.job("c")
    assert not held.get("export_id") and held["status"] != "exported" and held["revision"] == 1
    j = app.state.store.job("c")
    app.state.store.job("c", {**j, "reviewed": True})
    assert [r["id"] for r in check(client, owner, app, "c")["ready"]] == ["c"]
    response = export(client, owner, app, "c")
    assert response.status_code == 200, response.text
    assert column(book(client.get(response.json()["url"], headers=bearer(owner)))["Header"], "Transaction Number") == [1]


def test_a_batch_with_a_held_invoice_still_exports_nothing(client, owner, app, three):
    response = export(client, owner, app, *three)
    assert response.status_code == 409 and "held" in response.json()["detail"]
    assert not any(app.state.store.job(jid).get("export_id") for jid in three)


def test_an_exported_invoice_is_locked_against_a_second_real_export(client, owner, app, three):
    assert export(client, owner, app, "a").status_code == 200
    result = check(client, owner, app, "a", "b")
    assert [r["id"] for r in result["ready"]] == ["b"]
    assert [(h["id"], [r["code"] for r in h["reasons"]]) for h in result["held"]] == [("a", ["Exported"])]
    assert "already exported" in result["held"][0]["reasons"][0]["message"]
    response = export(client, owner, app, "a", "b")
    assert response.status_code == 409 and "already exported" in response.json()["detail"]
    assert not app.state.store.job("b").get("export_id")
    with app.state.store.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM exports WHERE job_id='a'").fetchone()[0] == 1


def test_the_same_invoice_twice_in_one_selection_is_held_as_a_duplicate(client, owner, app, tmp_path, three):
    job(client, owner, app, tmp_path, "a2", "INV-A")
    result = check(client, owner, app, "a", "a2")
    assert [r["id"] for r in result["ready"]] == ["a"]
    assert [(h["id"], [r["code"] for r in h["reasons"]]) for h in result["held"]] == [("a2", ["Duplicate"])]


def test_the_single_export_holds_only_the_template_sheets(client, owner, app, three):
    response = client.post("/api/jobs/a/export", headers=bearer(owner), json={"revision": 1})
    assert response.status_code == 200, response.text
    assert book(client.get(response.json()["url"], headers=bearer(owner))).sheetnames == list(OWNER_TEMPLATE)
    evidence = client.get(f"/api/exports/{response.json()['id']}/evidence", headers=bearer(owner))
    assert evidence.status_code == 200


def test_the_draft_of_everything_includes_held_ones_and_is_marked_draft_on_every_sheet(client, owner, app, three):
    response = client.post("/api/exports/target-draft", headers=bearer(owner), json=selection(app, *three))
    assert response.status_code == 200, response.text
    assert 'filename="DRAFT_' in response.headers["content-disposition"]
    assert response.headers["x-draft-included"] == "3"
    workbook = book(response)
    assert workbook.sheetnames == [*OWNER_TEMPLATE, "Checks"]  # the template's names: layout readers keep working
    for sheet in workbook.worksheets:
        assert "DRAFT" in sheet.oddHeader.center.text and sheet["A1"].comment.text.startswith("DRAFT")
        assert sheet.sheet_properties.tabColor.rgb.endswith("C00000")
    for name, columns in OWNER_TEMPLATE.items():
        assert [c.value for c in workbook[name][1]] == columns
    assert column(workbook["Header"], "Transaction Number") == [1, 2, 3]
    assert not any(app.state.store.job(jid).get("export_id") for jid in three)
