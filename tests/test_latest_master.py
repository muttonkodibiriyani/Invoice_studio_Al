"""Synthetic contracts for independent master blocks, strict matching and atomic final receipts."""
import io
import json
from datetime import date
from decimal import Decimal

import openpyxl
import pytest

from app import target_check
from app.excel import HEADERS, rules_workbook
from app.fine_rules import RowsSource, RulesConfig, Run, match_line, run_invoice
from app.fine_rules_source import LookupRulesSource
from app.master_catalog import HEADERS as MASTER_HEADERS, inspect_workbook, scalar
from app.master_orders import destination
from app.matching import rules_validation, rules_view
from app.models import Invoice, Line
from app.reference_lookup import _configured_columns, _record
from tests.conftest import bearer

HASH = "a" * 64


def master_bytes(edit=None):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Synthetic master"
    sheet.append([" " + h + " " for h in MASTER_HEADERS])
    # Adjacent blocks deliberately describe DIFFERENT items: positional joins would be wrong.
    sheet.append([11, "00012345678905", 100000001, "Rose red", "VPN-1", "Rose", "ABC001RA4AED", "Synthetic Co", 101,
                  1200, "Synthetic shop", "Shop", "MAN-2", 450002, "00012345678912", 100000002, 3])
    sheet.append([12, "00012345678912", 100000002, "Rose blue", "VPN-1", "Rose", "DEF001RA4AED", "Other Co", 202,
                  None, None, None, "MAN-1", 450001, "00012345678905", 100000001, 2])
    if edit:
        edit(sheet)
    result = io.BytesIO()
    book.save(result)
    return result.getvalue()


def activate(client, owner, content=None):
    response = client.post("/api/item-master/validate", headers=bearer(owner),
                           files={"file": ("master.xlsx", content or master_bytes())})
    assert response.status_code == 200, response.text
    upload = response.json()
    result = client.post(f"/api/item-master/uploads/{upload['upload_id']}/activate", headers=bearer(owner))
    assert result.status_code == 200, result.text
    return upload, result.json()


def config():
    return {"version": "approved-test", "location_master": {"1200": {"type": "Store (S)", "market": "ARE", "country": "ARE"}},
            "supplier_sites": [{"supplier_site": "101", "currency": "AED", "status": "Active", "supplier_name": "Synthetic Co", "supplier_code": "ABC001", "site_name": "ABC001RA4AED"}],
            "vat_codes": [{"region": "ARE", "code": "PV5", "rate": "5", "active_from": "2020-01-01"}]}


def invoice(number="INV-1", po="450001", location="1200", qty=2):
    return Invoice(number=number, date="2026-11-01", supplier_name="Synthetic Co", currency="AED", po=po,
                   location=location, net=100, tax=5,
                   lines=[Line(sku="VPN-1", description="Rose red", qty=qty, price=50)])


def run(store, inv=None, text=None):
    inv = inv or invoice()
    return run_invoice(inv, LookupRulesSource(store), RulesConfig.from_dict(config()),
                       text_value=text or f"Synthetic Co\nInvoice {inv.number}\nDate 2026-11-01\nDeliver To: 1200\nRose red 2 50 100\nNet 100\nVAT 5")


def rich_receipt(store, qty="2", identity="GRN-1", duplicate=False):
    data = {"EBS_SUPPLIER_CODE": "ABC001", "RMS_ORDER_NO": "450001", "LOCATION": "1200",
            "RMS_ITEM_ID": "100000001", "QTY_RECEIVED": qty, "TOTAL COST": "100", "CURRENCY_CODE": "AED",
            "RECEIPT_ID": identity}
    rows = [data, dict(data)] if duplicate else [data]
    configured = _configured_columns({"columns": {"po": {"po": ["RMS_ORDER_NO"], "internal_item": ["RMS_ITEM_ID"], "site": ["EBS_SUPPLIER_CODE"]}}})
    with store.connection(True) as c:
        for n, data in enumerate(rows, 2):
            raw = {"kind": "po", "source_hash": HASH, "source_sheet": "Authoritative GRNs", "source_row": n,
                   "keys": [], "data": data, "flags": []}
            p = _record(raw, configured)
            c.execute("INSERT INTO lookup_rows VALUES (?,?,?,?,?,?)", p[:6])
            c.executemany("INSERT INTO lookup_terms VALUES (?,?,?)", [(p[1], term, p[0]) for term in p[6]])
        c.execute("INSERT INTO lookup_sources VALUES (?,?)", ("rich", json.dumps({"id": "rich", "source_hashes": [HASH]})))


def test_import_independent_blocks_source_identity_and_configuration(client, owner, app):
    app.state.store.set("fine_rules_config", config())
    before = app.state.store.get("fine_rules_config")
    upload, _ = activate(client, owner)
    report = upload["report"]
    assert report["rows"] == 2 and report["blocks"] == {"item": 2, "po": 2} and report["locations"] == 1
    assert app.state.store.get("fine_rules_config") == before
    source = LookupRulesSource(app.state.store)
    item = source.items_by_vpn("VPN-1")
    assert {r["SUPPLIER"] for r in item} == {"101", "202"}
    orders = source.master_orders({"100000001"}, {"101"})
    assert [r["RMS_ORDER_NO"] for r in orders] == ["450001"]
    assert orders[0]["_ref"].endswith(":po:Synthetic master!3")
    assert orders[0]["_bridge_refs"][0].endswith(":item:Synthetic master!2")
    rows = target_check.store_rows(app.state.store)
    assert rows("Synthetic master", 2) is None  # Unqualified physical row is ambiguous across blocks.
    digest = upload["report"]["source_sha256"]
    assert rows(f"{digest}:item:Synthetic master", 2)["ITEM_PARENT"] == "100000001"
    assert rows(f"{digest}:po:Synthetic master", 2)["RMS_ITEM_ID"] == "100000002"


def test_snapshot_rollback_preserves_declared_legacy_semantics(client, owner, app):
    from app.reference_lookup import _terms
    raw = {"kind": "item", "source_hash": HASH, "source_sheet": "Legacy", "source_row": 2,
           "keys": [], "data": {"ITEM": "00012345678905", "ITEM_PARENT": "100000001", "SUPPLIER": "101"}, "flags": []}
    declared = _configured_columns({"columns": {"item": {"gtin": ["ITEM"], "internal_item": ["ITEM_PARENT"], "site": ["SUPPLIER"]}}})
    p = _record(raw, declared)
    with app.state.store.connection(True) as c:
        c.execute("INSERT INTO lookup_rows VALUES (?,?,?,?,?,?)", p[:6])
        c.executemany("INSERT INTO lookup_terms VALUES (?,?,?)", [(p[1], term, p[0]) for term in p[6]])
        c.execute("INSERT INTO lookup_sources VALUES (?,?)", ("legacy", json.dumps({"id": "legacy"})))
    activate(client, owner)
    state = client.get("/api/item-master", headers=bearer(owner)).json()
    snap = next(v for v in state["versions"] if v.get("source") == "snapshot")
    restored = client.post(f"/api/item-master/versions/{snap['id']}/rollback", params={"expected_signature": state["signature"]}, headers=bearer(owner))
    assert restored.status_code == 200, restored.text
    with app.state.store.connection() as c:
        stored = json.loads(c.execute("SELECT payload FROM lookup_rows").fetchone()[0])
        terms = {r[0] for r in c.execute("SELECT term FROM lookup_terms")}
    assert stored == json.loads(p[5])
    assert terms == set(_terms(raw, stored["candidate_fields"]))
    assert LookupRulesSource(app.state.store).items_by_barcode("00012345678905")


def test_stale_activation_failure_and_rollback_are_atomic(client, owner, app, monkeypatch):
    import app.master_catalog as module
    initial, _ = activate(client, owner)
    pending = client.post("/api/item-master/validate", headers=bearer(owner), files={"file": ("master.xlsx", master_bytes())}).json()
    before = client.get("/api/item-master", headers=bearer(owner)).json()
    real = module._record
    def fail(raw, columns, *args):
        if raw["kind"] == "po":
            raise RuntimeError("synthetic write failure")
        return real(raw, columns, *args)
    monkeypatch.setattr(module, "_record", fail)
    from fastapi.testclient import TestClient
    with TestClient(app, raise_server_exceptions=False) as safe:
        response = safe.post(f"/api/item-master/uploads/{pending['upload_id']}/activate", headers=bearer(owner))
    assert response.status_code == 500
    after = client.get("/api/item-master", headers=bearer(owner)).json()
    assert after["active"] == before["active"] and after["signature"] == before["signature"]
    monkeypatch.setattr(module, "_record", real)
    assert client.post(f"/api/item-master/uploads/{pending['upload_id']}/activate", headers=bearer(owner)).status_code == 200
    signature = client.get("/api/item-master", headers=bearer(owner)).json()["signature"]
    assert client.post(f"/api/item-master/versions/{initial['upload_id']}/rollback", params={"expected_signature": signature}, headers=bearer(owner)).status_code == 200
    assert client.post(f"/api/item-master/versions/{initial['upload_id']}/rollback", params={"expected_signature": signature}, headers=bearer(owner)).status_code == 409


def test_bounded_diagnostics_dates_formulas_duplicates_and_leading_zeros():
    def edit(sheet):
        sheet["E2"] = date(2026, 1, 1)
        sheet["I3"] = '=HYPERLINK("private-marker")'
        sheet.append([c.value for c in sheet[2]])
        sheet["A2"].number_format = "00000"
    records, metadata = inspect_workbook(master_bytes(edit))
    assert metadata["report"]["diagnostic_total"] >= 3
    assert "private-marker" not in json.dumps(metadata)
    assert any("quarantined" in f for r in records for f in r["flags"])
    assert records[0]["data"]["ITEM"] == "00011"
    book = openpyxl.Workbook()
    book.active["A1"] = 12345678901234567
    with pytest.raises(ValueError):
        scalar(book.active["A1"])
    book.active["A1"] = "0012345678901234567"
    assert scalar(book.active["A1"]) == "0012345678901234567"


@pytest.mark.parametrize("site,expected", [({"A"}, "P1"), ({"B"}, "P2"), ({"C"}, None), (None, None)])
def test_supplier_isolation_vpn_first_and_no_global_fallback(site, expected):
    source = RowsSource([{"VPN": "SAME-1", "ITEM_PARENT": "P1", "SUPPLIER SITE": "A", "BARCODE": "11111111", "ITEM_DESC": "Red"},
                         {"VPN": "SAME-1", "ITEM_PARENT": "P2", "SUPPLIER SITE": "B", "BARCODE": "22222222", "ITEM_DESC": "Blue"}])
    inv = Invoice(lines=[Line(sku="SAME-1", description="Red")])
    match = match_line(Run(inv, RulesConfig(), "synthetic.pdf"), 1, inv.lines[0], source, site)
    assert match["parent"] == expected
    if expected:
        assert match["method"] == "VPN exact"


def test_dedicated_conflict_is_not_masked_by_description_barcode():
    source = RowsSource([{"VPN": "VPN-1", "ITEM_PARENT": "P1", "SUPPLIER": "A", "BARCODE": "00012345678905", "ITEM_DESC": "red"},
                         {"VPN": "VPN-2", "ITEM_PARENT": "P2", "SUPPLIER": "A", "BARCODE": "00012345678912", "ITEM_DESC": "blue"}])
    inv = Invoice(lines=[Line(sku="VPN-1", gtin="00012345678912", description="red 00012345678905")])
    match = match_line(Run(inv, RulesConfig(), "synthetic.pdf"), 1, inv.lines[0], source, {"A"})
    assert match["status"] == "Exception" and match["parent"] is None


def test_blank_variant_does_not_exclude_compatible_parent():
    from app.fine_rules import variant_parents
    rows = [{"ITEM_PARENT": "P1", "SUPP_COLOR": "red"}, {"ITEM_PARENT": "P2", "SUPP_COLOR": ""}]
    assert variant_parents(Line(description="Rose red"), rows, {"P1", "P2"}) == {"P1", "P2"}


def test_barcode_fallback_bracketed_vpn_and_pair_quantity_unchanged():
    source = RowsSource([{"VPN": "STYLE-1", "ITEM_PARENT": "P1", "SUPPLIER": "A", "BARCODE": "00012345678905", "ITEM_DESC": "Socks 30PAIR"}]*2)
    for line, method in [(Line(description="[STYLE-1] Socks 30PAIR", qty=30, uom="PR"), "VPN exact"),
                         (Line(gtin="00012345678905", description="Socks 30PAIR", qty=30, uom="PR"), "Barcode exact")]:
        result = match_line(Run(Invoice(lines=[line]), RulesConfig(), "synthetic.pdf"), 1, line, source, {"A"})
        assert result["parent"] == "P1" and result["method"] == method and line.qty == 30


def test_master_order_manual_bridge_location_and_unknown_receipt_hard_hold(client, owner, app):
    activate(client, owner)
    for po in ("450001", "MAN-1", "UNRECOGNIZED", None):
        result = run(app.state.store, invoice(po=po))
        assert result["header"]["Order No"] == "450001"
        assert result["header"]["Location"] == "1200"
        assert result["lines"][0]["Item"] == "100000001"
        assert result["lines"][0]["Quantity"] == 2 and result["lines"][0]["Unit Cost"] == 50
        view = rules_view(result, "", [])
        validation = rules_validation(view, reviewed=True)
        assert not validation["ready"] and any(i["level"] == "hard" and i["code"] == "Receipt Evidence" for i in validation["issues"])
        assert validation["allocations"] == []


def test_location_does_not_match_phone_or_bill_to_and_warehouse_preserved():
    directory = [{"source_hash": HASH, "source_sheet": "Master", "source_row": 2,
                  "data": {"LOCATION NUMBER": "12", "LOCATION NAME": "Warehouse"}}]
    assert destination(Invoice(location="12"), {"pages": {1: "Supplier Tel: 123456789\nBill To: Buyer"}}, directory, RulesConfig()) is None
    result = destination(Invoice(location="12"), {"pages": {1: "Deliver To: 12"}}, directory, RulesConfig())
    assert result["kind"] == "Warehouse (W)"
    assert destination(Invoice(location="12"), {"pages": {1: "Deliver To: 12"}}, directory,
                       RulesConfig(location_master={"12": {"type": "Store (S)"}})) is None


def test_richer_receipts_survive_activation_and_reserve_once(client, owner, app):
    rich_receipt(app.state.store, duplicate=True)
    activate(client, owner)
    result = run(app.state.store)
    controls = result["receipt_controls"]
    assert controls["known"] and len(controls["capacities"]) == 1
    assert controls["capacities"][0]["qty"] == "2"
    view = rules_view(result, "", [])
    view["issues"] = [i for i in view["issues"] if i["code"] != "Missing Evidence"]  # Test ledger independently of printed-location evidence.
    first = rules_validation(view, reviewed=True)
    assert first["ready"] and first["allocations"][0]["qty"] == "2"
    second = rules_validation(view, [{"invoice_key": "different-invoice", "allocations": first["allocations"]}], reviewed=True)
    assert not second["ready"] and second["allocations"] == []
    assert any(i["code"] == "Receipt Conflict" for i in second["issues"])


def test_rollback_to_master_restores_full_richer_receipt_composition(client, owner, app):
    rich_receipt(app.state.store)
    first, _ = activate(client, owner)
    activate(client, owner)
    current = client.get("/api/item-master", headers=bearer(owner)).json()
    response = client.post(f"/api/item-master/versions/{first['upload_id']}/rollback",
                           params={"expected_signature": current["signature"]}, headers=bearer(owner))
    assert response.status_code == 200, response.text
    result = run(app.state.store)
    assert result["receipt_controls"]["known"]
    assert result["receipt_controls"]["capacities"][0]["qty"] == "2"
    with app.state.store.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM lookup_sources WHERE id='rich'").fetchone()[0] == 1


def test_receipt_identity_cannot_be_consumed_again_at_another_family_site():
    from app.master_orders import receipt_capacities
    rows = [{"EBS_SUPPLIER_CODE": "ABC001", "RMS_ITEM_ID": "100000001", "RECEIPT_ID": "GRN-1", "QTY_RECEIVED": "2"}]
    a = receipt_capacities(rows, "SITE-A", "450001", "1200", {"100000001": Decimal(2)})
    b = receipt_capacities(rows, "SITE-B", "450001", "1200", {"100000001": Decimal(2)})
    assert a["known"] and b["known"]
    assert a["capacities"][0]["key"] == b["capacities"][0]["key"]


def evidenced(value):
    return {"value": value, "flagged": False, "evidence": [{"source": "synthetic approved evidence"}], "label": "Field"}


def test_reopened_selected_workbook_exact_layout_types_formats_blanks_and_keys():
    def view(number, item, count):
        return {"status": "Approved", "fields": {k: evidenced(v) for k, v in {
            "number": number, "site": "00101", "po": "450001", "location": "1200", "location_type": "Store (S)",
            "date": "2026-11-01", "net": str(50*count), "tax": str(Decimal('2.5')*count), "taxCode": "PV5"}.items()},
                "lines": [{"line": n, "cells": {k: evidenced(v) for k, v in {
                    "Item": item, "UPC": "00012345678905", "Unit Cost": "50", "Quantity": "1", "Unit Tax Code": "PV5"}.items()}} for n in range(1,count+1)]}
    content, _ = rules_workbook([view("=CMD()", "100000001", 2), view("INV-2", "100000002", 1)])
    book = openpyxl.load_workbook(io.BytesIO(content))
    for name, headers in HEADERS.items():
        assert [c.value for c in book[name][1]] == headers
    assert [book["Header"].cell(n,1).value for n in (2,3)] == [1,2]
    assert [book["Details"].cell(n,1).value for n in (2,3,4)] == [1,1,2]
    assert book["Header"]["B2"].data_type == 's' and book["Header"]["C2"].value == "00101"
    assert isinstance(book["Header"]["G2"].value, (date,))
    assert all(book["Header"].cell(n,c).value is None for n in (2,3) for c in (10,11,12,13))
    assert all(book["Details"].cell(n,3).value is None for n in (2,3,4))
    assert all(book["Details"].cell(n,c).number_format == "0.0000" for n in (2,3,4) for c in (4,5))
    assert book["Header"]["H2"].number_format == "0.0000" and book["Tax_Breakdown"]["C2"].value == 100


def test_ambiguous_vpn_requires_exact_corroboration_and_description_is_review_only():
    source = RowsSource([{"VPN": "SAME-1", "ITEM_PARENT": "P1", "SUPPLIER": "A", "BARCODE": "00012345678905", "ITEM_DESC": "Red rose"},
                         {"VPN": "SAME-1", "ITEM_PARENT": "P2", "SUPPLIER": "A", "BARCODE": "00012345678912", "ITEM_DESC": "Blue rose"}])
    for line, parent in [(Line(sku="SAME-1", description="Rose"), None),
                         (Line(sku="SAME-1", gtin="00012345678905", description="Red rose"), "P1")]:
        matched = match_line(Run(Invoice(lines=[line]), RulesConfig(), "synthetic.pdf"), 1, line, source, {"A"})
        assert matched["parent"] == parent
    line = Line(description="Rose red", qty=2)
    candidate = match_line(Run(Invoice(lines=[line]), RulesConfig(), "synthetic.pdf"), 1, line, source, {"A"})
    assert candidate["parent"] is None and candidate["method"] == "Description candidate"


def test_unrelated_supplier_ambiguity_is_not_resolved_by_global_item_search():
    source = RowsSource([{"VPN": "ONE-1", "ITEM_PARENT": "P1", "SUPPLIER": "A", "SUPPLIER_NAME": "Synthetic Co", "ITEM_DESC": "Rose"},
                         {"VPN": "TWO-2", "ITEM_PARENT": "P2", "SUPPLIER": "B", "SUPPLIER_NAME": "Synthetic Co", "ITEM_DESC": "Blue"}])
    inv = Invoice(supplier_name="Synthetic Co", lines=[Line(sku="ONE-1", description="Rose")])
    result = run_invoice(inv, source, text_value="Synthetic Co\nINVOICE\nRose")
    assert result["header"]["Supplier Site"] == ""
    assert result["lines"][0]["Item"] == ""


def test_receipt_final_edited_quantity_and_scope_must_match_capacity(client, owner, app):
    rich_receipt(app.state.store)
    activate(client, owner)
    view = rules_view(run(app.state.store), "", [])
    view["issues"] = []  # Isolate reservation logic from printed cell verification.
    view["fields"]["po"]["value"] = "ANOTHER-ORDER"
    assert not rules_validation(view, reviewed=True)["ready"]
    view["fields"]["po"]["value"] = "450001"
    view["lines"][0]["cells"]["Quantity"]["value"] = "3"
    assert not rules_validation(view, reviewed=True)["ready"]


def test_item_master_body_limit_is_endpoint_specific(client, owner, monkeypatch):
    import app.master_catalog as module
    called=[]
    def inspected(content):
        called.append(len(content))
        raise module.HTTPException(400, "synthetic parser reached")
    monkeypatch.setattr(module, "inspect_workbook", inspected)
    content=b"x"*12_600_000
    accepted=client.post("/api/item-master/validate", headers=bearer(owner), files={"file": ("master.xlsx",content)})
    assert accepted.status_code==400 and called==[len(content)]
    refused=client.post("/api/invoices", headers=bearer(owner), files={"file": ("invoice.pdf",content)})
    assert refused.status_code==413


@pytest.mark.parametrize("description", ["VPN: ROSE", "[ROSE] Synthetic item", "VPN: STYLE-A"])
def test_explicit_alphabetic_vpn_is_not_rejected(description):
    from app.fine_rules import vpn_candidates
    values=vpn_candidates(Line(description=description))
    assert values and values[0]["value"] in {"ROSE","STYLE-A"}


def test_owner_target_entries_cannot_bypass_missing_master_receipt_association(client, owner, app):
    activate(client, owner)
    result=run(app.state.store, invoice(po="UNKNOWN",qty=99))
    assert result["receipt_controls"] == {"known":False,"capacities":[],"demand":{}}
    entries={"header":{"site":"101","po":"450001","location":"1200","location_type":"Store (S)","taxCode":"PV5"},
             "lines":{"1":{"Item":"100000001","Quantity":"99","Unit Cost":"50","Unit Tax Code":"PV5"}}}
    view=rules_view(result,"",[],entries=entries)
    validation=rules_validation(view,reviewed=True)
    assert not validation["ready"] and validation["allocations"] == []
    assert any(i["level"]=="hard" and i["code"]=="Receipt Evidence" for i in validation["issues"])
