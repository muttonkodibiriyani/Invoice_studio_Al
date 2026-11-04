"""Not-in-item-master evidence: every Not found / Ambiguous / Conflict Item cell keeps its lookup trace."""
import json

from app.evidence import TRACE_ROWS
from app.fine_rules import RowsSource, run_invoice
from app.matching import rules_view
from app.models import Invoice, Line
from tests.conftest import bearer
from tests.test_latest_master import activate

SUPPLIER = {"SUPPLIER": "A", "SUPPLIER_NAME": "Synthetic Co"}
ROWS = [{**SUPPLIER, "VPN": "ONE-1", "ITEM_PARENT": "P1", "BARCODE": "00012345678905", "ITEM_DESC": "Red rose"},
        {**SUPPLIER, "VPN": "ONE-1", "ITEM_PARENT": "P2", "BARCODE": "00012345678912", "ITEM_DESC": "Blue rose"},
        {**SUPPLIER, "VPN": "TWO-2", "ITEM_PARENT": "P3", "BARCODE": "00012345678929", "ITEM_DESC": "Tulip"}]


def view(lines, rows=ROWS, **kwargs):
    inv = Invoice(number="I1", supplier_name="Synthetic Co", lines=lines)
    result = run_invoice(inv, RowsSource(rows), text_value="Synthetic Co\nINVOICE I1")
    return rules_view(json.loads(json.dumps(result, default=str)), "", [], **kwargs)


def trace(v, n):
    return v["lines"][n - 1]["cells"]["Item"].get("lookup_trace")


def test_not_in_item_master_keeps_printed_values_columns_supplier_and_no_rows():
    t = trace(view([Line(sku="ZZZ-9", gtin="00012345678936", description="Widget")]), 1)
    assert t["state"] == "Not in item master" and t["rule"] == "R-003"
    assert t["printed"] == {"barcode": ["00012345678936"], "vpn": ["ZZZ-9"], "description": "Widget"}
    assert t["searched"] == {"columns": ["BARCODE", "VPN", "ITEM_DESC"], "supplier_sites": ["A"]}
    assert t["rows"] == [] and t["rows_total"] == 0
    assert t["item_master_version"] is None and t["manual_fix"] is None


def test_ambiguous_and_conflict_list_the_candidate_rows():
    v = view([Line(sku="ONE-1", description="Rose"), Line(sku="TWO-2", gtin="00012345678905", description="Tulip")])
    ambiguous, conflict = trace(v, 1), trace(v, 2)
    assert ambiguous["state"] == "Ambiguous" and ambiguous["rows"] == ["P1", "P2"] and ambiguous["rows_total"] == 2
    assert ambiguous["searched"]["columns"] == ["VPN"]
    assert conflict["state"] == "Conflict" and conflict["rule"] == "ALG-020" and conflict["rows"] == ["P1", "P3"]


def test_matched_line_has_no_trace():
    assert trace(view([Line(sku="TWO-2", description="Tulip")]), 1) is None


def test_rows_are_capped_and_the_total_kept():
    rows = [{**SUPPLIER, "VPN": "MANY", "ITEM_PARENT": f"P{n}", "ITEM_DESC": "x"} for n in range(TRACE_ROWS + 4)]
    t = trace(view([Line(sku="MANY", description="Thing")], rows), 1)
    assert len(t["rows"]) == TRACE_ROWS and t["rows_total"] == TRACE_ROWS + 4


def test_manual_fix_and_item_master_version_are_recorded():
    v = view([Line(sku="ZZZ-9", description="Widget")], entries={"lines": {"1": {"Item": "100000001"}}},
             master_version="v-1")
    t = trace(v, 1)
    assert t["manual_fix"] == {"kind": "entered", "value": "100000001"}
    assert t["item_master_version"] == "v-1"


def test_review_api_returns_the_trace_with_the_active_version(client, owner, app):
    _, active = activate(client, owner)
    app.state.store.job("nf", {"id": "nf", "revision": 1, "status": "review", "filename": "a.pdf", "text": "",
                               "boxes": [], "invoice": Invoice(number="I1", supplier_name="Synthetic Co", lines=[
                                   Line(sku="NOPE-1", description="Widget", qty=1, price=1)]).model_dump(mode="json")})
    response = client.get("/api/jobs/nf", headers=bearer(owner))
    assert response.status_code == 200, response.text
    t = response.json()["rules"]["lines"][0]["cells"]["Item"]["lookup_trace"]
    assert t["item_master_version"] == active["active"] and t["printed"]["vpn"] == ["NOPE-1"]
