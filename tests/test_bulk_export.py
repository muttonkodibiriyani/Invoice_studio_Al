"""Owner decisions A (5 Nov) and D11: bulk export of the selected invoices.

D11: 'don't invent, I just need information in the excel, export team will validate manually'. The real export
writes what was found for every selected invoice into the three template sheets only, Transaction Number 1..n, blanks
blank. Validation and review holds no longer block it; they travel in each receipt as the open holds. Only integrity
blocks: still processing or not read, changed since the check, already exported, the same invoice twice. The draft of
everything is marked DRAFT in its file name and on every sheet, with the Checks sheet.
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


def job(client, owner, app, tmp_path, jid, number, reviewed=True, held=False):
    """An invoice whose current rules view has every target cell evidenced and no issue. ``held``: no receipt evidence,
    no evidenced Location (Missing Evidence) and never reviewed, the holds that used to block the real export."""
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
    rules = {k: v for k, v in j["rules"].items() if k != "receipt_controls"} | {"issues": [], "lines": lines, "fields": fields}
    if held:
        fields["location"] = {**fields["location"], "value": None, "evidence": [], "flagged": True}
        rules["issues"] = [{"code": "Missing Evidence", "message": "Location has no evidence", "field": "Location",
                            "owner": "Accounts payable", "line": None, "rule": "EVIDENCE", "evidence": "", "blocking": True,
                            "level": "hard"}]
        rules["receipt_controls"] = {"known": False, "capacities": []}
    store.job(jid, {**j, "rules": rules, "reviewed": reviewed})
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


def receipts(app, jid):
    with app.state.store.connection() as c:
        return [json.loads(r[0]) for r in c.execute("SELECT payload FROM exports WHERE job_id=?", (jid,))]


@pytest.fixture
def three(client, owner, app, tmp_path):
    return (job(client, owner, app, tmp_path, "a", "INV-A"), job(client, owner, app, tmp_path, "b", "INV-B"),
            job(client, owner, app, tmp_path, "c", "INV-C", reviewed=False, held=True))


def test_the_check_lists_open_holds_as_attention_not_as_blockers_and_changes_nothing(client, owner, app, three):
    before = {jid: app.state.store.job(jid)["revision"] for jid in three}
    result = check(client, owner, app, *three)
    assert [r["id"] for r in result["ready"]] == ["a", "b", "c"] and result["blocked"] == []
    assert [r["transaction_number"] for r in result["ready"]] == [1, 2, 3]
    attention = {h["code"] for h in result["ready"][2]["attention"]}
    assert {"Receipt Evidence", "Missing Evidence", "REVIEW"} <= attention
    assert result["ready"][0]["attention"] == []
    assert {jid: app.state.store.job(jid)["revision"] for jid in three} == before
    assert not any(app.state.store.job(jid).get("export_id") for jid in three)


def test_a_held_invoice_is_exported_with_what_was_found_and_its_blanks(client, owner, app, three):
    response = export(client, owner, app, *three)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 3
    workbook = book(client.get(response.json()["url"], headers=bearer(owner)))
    assert workbook.sheetnames == list(OWNER_TEMPLATE)  # no Checks sheet in the real export
    for name, columns in OWNER_TEMPLATE.items():
        assert [c.value for c in workbook[name][1]] == columns
    header = workbook["Header"]
    assert column(header, "Transaction Number") == [1, 2, 3]
    assert column(workbook["Tax_Breakdown"], "Transaction Number") == [1, 2, 3]
    assert column(workbook["Details"], "Transaction Number") == [1, 1, 2, 2, 3, 3]
    assert column(header, "Document") == ["INV-A", "INV-B", "INV-C"]
    assert column(header, "Location") == [1001, 1001, None]  # found values written, the gap stays blank
    assert app.state.store.job("c")["status"] == "exported"


def test_the_receipt_records_the_open_holds_and_the_owner_decision(client, owner, app, three):
    assert export(client, owner, app, *three).status_code == 200
    [receipt] = receipts(app, "c")
    assert receipt["validation"] == "exported unvalidated by owner decision D11"
    holds = {(h["code"], h["cell"]) for h in receipt["open_holds"]}
    assert {("Receipt Evidence", None), ("Missing Evidence", "Location"), ("REVIEW", None)} <= holds
    assert all(h["message"] for h in receipt["open_holds"])
    assert receipt["transaction_number"] == 3 and {"counts", "checks"} <= set(receipt["target_check"])
    [clean] = receipts(app, "a")
    assert clean["open_holds"] == [] and clean["validation"] == "exported unvalidated by owner decision D11"


def test_the_real_export_has_the_cells_of_the_draft_of_everything(client, owner, app, three):
    draft = book(client.post("/api/exports/target-draft", headers=bearer(owner), json=selection(app, *three)))
    response = export(client, owner, app, *three)
    real = book(client.get(response.json()["url"], headers=bearer(owner)))
    for name in OWNER_TEMPLATE:
        assert [[c.value for c in row] for row in real[name].iter_rows()] == \
               [[c.value for c in row] for row in draft[name].iter_rows()]
    for sheet in real.worksheets:
        assert sheet["A1"].comment is None and "DRAFT" not in (sheet.oddHeader.center.text or "")


def test_a_processing_or_changed_invoice_is_not_exported(client, owner, app, three):
    store = app.state.store
    store.job("b", {**store.job("b"), "status": "processing"})
    body = selection(app, "a", "b", "c")
    body["jobs"][2]["revision"] = 0  # changed since the check
    result = client.post("/api/exports/batch/check", headers=bearer(owner), json=body).json()
    assert [r["id"] for r in result["ready"]] == ["a"]
    assert [(h["id"], [r["code"] for r in h["reasons"]]) for h in result["blocked"]] == [("b", ["Processing"]), ("c", ["Changed"])]
    response = client.post("/api/exports/batch", headers=bearer(owner), json=body)
    assert response.status_code == 409
    assert not any(store.job(jid).get("export_id") for jid in three)


def test_an_exported_invoice_is_locked_against_a_second_real_export(client, owner, app, three):
    assert export(client, owner, app, "c").status_code == 200
    result = check(client, owner, app, "c", "b")
    assert [r["id"] for r in result["ready"]] == ["b"]
    assert [(h["id"], [r["code"] for r in h["reasons"]]) for h in result["blocked"]] == [("c", ["Exported"])]
    assert "already exported" in result["blocked"][0]["reasons"][0]["message"]
    response = export(client, owner, app, "c", "b")
    assert response.status_code == 409 and "already exported" in response.json()["detail"]
    assert not app.state.store.job("b").get("export_id")
    assert len(receipts(app, "c")) == 1


def test_the_same_invoice_twice_in_one_selection_is_blocked_as_a_duplicate(client, owner, app, tmp_path, three):
    job(client, owner, app, tmp_path, "a2", "INV-A")
    result = check(client, owner, app, "a", "a2")
    assert [r["id"] for r in result["ready"]] == ["a"]
    assert [(h["id"], [r["code"] for r in h["reasons"]]) for h in result["blocked"]] == [("a2", ["Duplicate"])]
    assert export(client, owner, app, "a", "a2").status_code == 409
    assert not app.state.store.job("a").get("export_id")


def test_the_single_export_takes_a_held_invoice_with_only_the_template_sheets(client, owner, app, three):
    response = client.post("/api/jobs/c/export", headers=bearer(owner), json={"revision": 1})
    assert response.status_code == 200, response.text
    assert book(client.get(response.json()["url"], headers=bearer(owner))).sheetnames == list(OWNER_TEMPLATE)
    assert ("Missing Evidence", "Location") in {(h["code"], h["cell"]) for h in receipts(app, "c")[0]["open_holds"]}
    evidence = client.get(f"/api/exports/{response.json()['id']}/evidence", headers=bearer(owner))
    assert evidence.status_code == 200
    again = client.post("/api/jobs/c/export", headers=bearer(owner), json={"revision": 2})
    assert again.json()["id"] == response.json()["id"] and len(receipts(app, "c")) == 1


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


BY_OWNER_RULE = {"Ref No. 1", "Ref No. 2", "Ref No. 3", "Comment", "UPC"}


def blanks_written(workbook, transaction):
    """The empty cells the exported file holds for one transaction, read back from the file itself."""
    count = 0
    for sheet in workbook.worksheets:
        names = [c.value for c in sheet[1]]
        for row in sheet.iter_rows(min_row=2):
            if row[0].value == transaction:
                count += sum(c.value in (None, "") and name not in BY_OWNER_RULE for name, c in zip(names, row))
    return count


def edit_rules(app, jid, change):
    store = app.state.store
    j = store.job(jid)
    change(j["rules"])
    store.job(jid, j)


def test_a_clean_invoice_reports_no_blank_fields_though_ref_comment_and_upc_are_empty(client, owner, app, three):
    [item] = check(client, owner, app, "a")["ready"]
    assert item["blank_fields"] == 0 and item["status"] == app.state.store.job("a")["rules"]["status"]
    workbook = book(client.get(export(client, owner, app, "a").json()["url"], headers=bearer(owner)))
    assert column(workbook["Header"], "Comment") == [None] and column(workbook["Details"], "UPC") == [None, None]


def test_blank_fields_are_the_cells_the_exported_file_leaves_empty(client, owner, app, three):
    # The inbox's empty_flagged count misses a filled value with no evidence and a flagged value: both are written blank.
    def change(rules):
        rules["status"] = "Blocked"
        rules["fields"]["site"]["evidence"] = []  # filled, no evidence
        rules["fields"]["po"]["flagged"] = True  # filled, flagged
        rules["fields"]["net"]["value"] = None  # two cells: Total Cost Ex Tax and Tax Basis
        rules["lines"][1]["cells"]["Quantity"]["value"] = None
    edit_rules(app, "b", change)
    items = {item["id"]: item for item in check(client, owner, app, *three)["ready"]}
    assert (items["b"]["status"], items["b"]["blank_fields"]) == ("Blocked", 5)
    assert items["a"]["blank_fields"] == 0 and items["c"]["blank_fields"] == 1  # c: no Location
    workbook = book(client.get(export(client, owner, app, *three).json()["url"], headers=bearer(owner)))
    assert {jid: blanks_written(workbook, items[jid]["transaction_number"]) for jid in three} == \
           {jid: items[jid]["blank_fields"] for jid in three}


def test_an_empty_blocked_invoice_counts_every_blank_and_the_single_export_still_goes_through(client, owner, app, three):
    def change(rules):
        rules["status"] = "Blocked"
        for field in rules["fields"].values():
            field.update(value=None, evidence=[], flagged=True)
        for line in rules["lines"]:
            for cell in line["cells"].values():
                cell.update(value=None, evidence=[], flagged=True)
    edit_rules(app, "a", change)
    [item] = check(client, owner, app, "a")["ready"]
    assert item["status"] == "Blocked" and item["blank_fields"] == 8 + 2 + 2 * 4  # D11: asked about, never refused
    response = client.post("/api/jobs/a/export", headers=bearer(owner), json={"revision": app.state.store.job("a")["revision"]})
    assert response.status_code == 200, response.text
    workbook = book(client.get(response.json()["url"], headers=bearer(owner)))
    assert blanks_written(workbook, 1) == item["blank_fields"]


def test_a_blank_upc_counts_only_when_the_export_mode_writes_barcodes(client, owner, app, three):
    from app.excel import blank_fields
    view = app.state.store.job("a")["rules"]
    view["lines"][0]["cells"]["UPC"]["value"] = None
    assert blank_fields(view, "empty") == 0 and blank_fields(view, "barcode") == 1
