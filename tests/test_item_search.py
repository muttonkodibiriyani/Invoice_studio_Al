"""The installed item master is the rules' item search in production: the job child asks the API process, which
searches lookup_rows/lookup_terms, and matching.lookup_item runs per line. Synthetic item master only."""
import io
import json
import pathlib

import openpyxl
import pytest

from app import ai_context, ai_rules, engines, item_rules, item_search, matching
from app.fine_rules_source import LookupRulesSource
from app.master_catalog import COLUMNS, HEADERS, inspect_workbook, install
from app.models import Invoice, ProcessingOptions
from app.reference_lookup import ReferenceLookup
from app.store import Store

ROOT = pathlib.Path(__file__).resolve().parents[1]
# ITEM, BARCODE, ITEM_PARENT, ITEM_DESC, VPN, BRAND, SUPPLIER_NAME, SUPPLIER SITE NAME, SUPPLIER SITE,
# LOCATION NUMBER, LOCATION NAME, LOCATION NAME 2, MANUAL ORDER NO, RMS_ORDER_NO, BARCODE_1, RMS_ITEM_ID, QTY_RECEIVED
ROWS = [
    [11, "00012345678905", 100000001, "Synthetic rose hand cream 50 ml", "VPN-1", "Rose", "SYN001RA4AED",
     "Synthetic Co", 101, None, None, None, "MAN-2", 450002, "00012345678912", 100000002, 3],
    [12, "00012345678912", 100000002, "Synthetic rose body lotion 200 ml", "VPN-2", "Rose", "SYN001RA4AED",
     "Synthetic Co", 101, None, None, None, "MAN-1", 450001, "00012345678929", 100000003, 2.5],
    # The item master writes some barcodes with a ULT_ prefix (R-004). SYN001RA4AED has a second site, 102.
    [13, "ULT_00012345678936", 100000004, "Synthetic tulip soap", "VPN-4", "Tulip", "SYN001RA4AED",
     "Synthetic Tulip Co", 102, None, None, None, None, None, None, None, None],
    [14, "00012345678943", 100000005, "Synthetic tulip soap", "VPN-4", "Tulip", "SYN001RA4AED",
     "Synthetic Co", 101, None, None, None, None, None, None, None, None],
]


def master(rows=ROWS):
    book = openpyxl.Workbook()
    book.active.append(list(HEADERS))
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path)
    ReferenceLookup(store)  # the catalog tables, as the app creates them at start
    return store


def installed(store):
    records, _ = inspect_workbook(master())
    with store.connection(True) as c:
        install(store, c, records, {"id": "master:synthetic", "master_catalog": {"columns": COLUMNS}})
    return LookupRulesSource(store)


@pytest.mark.parametrize("line, column, match, parents", [
    ({"sku": "vpn 1"}, "VPN", "exact", ["100000001"]),
    ({"sku": "NO-SUCH", "part_code": "VPN-2"}, "VPN", "exact", ["100000002"]),
    ({"gtin": "00012345678912"}, "BARCODE", "exact", ["100000002"]),
    ({"barcode_unchecked": "00012345678905"}, "BARCODE", "exact", ["100000001"]),
    ({"gtin": "00012345678936"}, "BARCODE", "exact", ["100000004"]),
    ({"gtin": "ULT_00012345678936"}, "BARCODE", "exact", ["100000004"]),
    ({"sku": "VPN-4"}, "VPN", "exact", ["100000004", "100000005"]),
    ({"sku": "VPN-4", "supplier_site": "101"}, "VPN", "exact", ["100000005"]),
    ({"gtin": "00012345678936", "supplier_site": "101"}, "BARCODE", "exact", []),
    ({"description": "tulip soap", "supplier_site": "102"}, "ITEM_DESC", "fuzzy", ["100000004"]),
    ({"gtin": "00012345678912"}, "BARCODE_1", "exact", ["100000002"]),
    # Best first: the lotion row shares the most of this line, then the cream, then the "synthetic" soaps.
    ({"description": "Rose body lotion 200ml synthetic"}, "ITEM_DESC", "fuzzy",
     ["100000002", "100000001", "100000004", "100000005"]),
    ({"description": "Unrelated widget"}, "ITEM_DESC", "fuzzy", []),
    ({"qty": "2.50"}, "QTY_RECEIVED", "number", ["100000003"]),
    ({"qty": "3"}, "QTY_RECEIVED", "number", ["100000002"]),
    ({"qty": "4"}, "QTY_RECEIVED", "number", []),
    ({}, "VPN", "exact", []),
])
def test_a_search_returns_the_item_parents_of_one_column_best_first(store, line, column, match, parents):
    assert item_search.search(installed(store), line, column, match) == parents


def test_a_column_the_search_cannot_run_is_refused(store):
    with pytest.raises(item_rules.RulesError):
        item_search.search(installed(store), {"sku": "VPN-1"}, "VPN", "fuzzy")


def test_the_workbook_rules_run_over_the_installed_catalog(store):
    source = installed(store)
    lookup = lambda line: matching.lookup_item(lambda c, m: item_search.search(source, line, c, m), item_rules.rules())
    assert lookup({"sku": "VPN-1"})["status"] == "Found"
    assert lookup({"sku": "VPN-1", "gtin": "00012345678912"})["status"] == "Conflict"
    assert lookup({"description": "synthetic rose lotion"})["status"] == "Suggestion"
    assert lookup({"sku": "NONE"})["status"] == "Not in item master"


@pytest.mark.parametrize("invoice, sites", [
    ({"site": "102", "supplier_name": "Synthetic Co"}, ["102"]),
    ({"supplier_name": "synthetic co"}, ["101"]),
    ({"supplier_name": "SYN001RA4AED"}, ["101", "102"]),
    ({"supplier_name": "Nobody"}, []),
    ({}, []),
])
def test_an_invoice_resolves_its_supplier_sites_from_its_site_or_supplier_name(store, invoice, sites):
    assert item_search.invoice_sites(installed(store), invoice) == sites


@pytest.mark.parametrize("site, sites, limited", [
    ("102", ["101"], "102"),     # the reviewer's pick wins
    (None, ["101"], "101"),      # the invoice's one resolved site
    (None, ["101", "102"], None),  # several: unlimited
    (None, [], None),            # none: unlimited
])
def test_an_invoice_search_is_limited_to_its_one_supplier_site(site, sites, limited):
    asked = []
    search = item_search.for_invoice(lambda line, column, match: asked.append(line) or [], lambda: sites, site=site)
    search({"sku": "VPN-4", "price": "5.00"})("VPN", "exact")
    assert asked == [{**dict.fromkeys(item_search.LINE_FIELDS), "sku": "VPN-4", "supplier_site": limited}]


def job(tmp_path, monkeypatch, store, lines, supplier_name="Synthetic Co", supplier_site=None):
    read = {"text": "INVOICE SYN-1\nTotal 10.00\n", "boxes": [], "tables": [], "extraction_method": "template",
            "invoice": {"number": "SYN-1", "supplier_name": supplier_name, "currency": "AED", "date": "2026-10-01",
                        "net": "10.00", "tax": "0.50", "lines": lines}}
    monkeypatch.setattr(engines, "local_read", lambda *a, **k: json.loads(json.dumps(read)))
    monkeypatch.setattr(engines, "capabilities", lambda: [
        {"id": e, "installed": e == "invoice2data", "models_blocked": False, "version": None, "note": ""}
        for e in ("invoice2data", "paddleocr", "docling")])
    options = ProcessingOptions(engine="auto", ai_fallback=False)
    result = engines.process_job(ROOT / "samples" / "invoice.pdf", options, store, None, filename="SYN-1.pdf",
                                 supplier_site=supplier_site)
    return result, next(t for t in result["trace"] if t["engine"] == "item_rules")


LINE = {"sku": "VPN-1", "description": "Rose cream", "qty": "2", "price": "5.00", "net_amount": "10.00"}


def test_a_job_looks_its_lines_up_in_the_installed_item_master(tmp_path, monkeypatch, store):
    installed(store)
    result, trace = job(tmp_path, monkeypatch, store, [LINE])
    assert result["invoice"]["lines"][0]["item_id"] == "100000001"
    assert trace["fields"] == ["line 1 item_id"] and "reason" not in trace


def test_with_no_item_master_installed_a_line_is_not_in_the_item_master(tmp_path, monkeypatch, store):
    result, trace = job(tmp_path, monkeypatch, store, [LINE])
    assert not result["invoice"]["lines"][0].get("item_id")
    assert [h["status"] for h in trace["held"]] == ["Not in item master"] and "reason" not in trace


def test_a_failed_search_stops_the_item_rules_not_the_read(tmp_path, monkeypatch, store):
    monkeypatch.setattr(item_search, "search", lambda *a: 1 / 0)
    result, trace = job(tmp_path, monkeypatch, store, [LINE])
    assert trace["status"] == "unavailable" and "ZeroDivisionError" in trace["reason"]
    assert result["invoice"]["number"] == "SYN-1"


@pytest.mark.parametrize("supplier_name, supplier_site, item_id, held", [
    ("Synthetic Co", None, "100000005", []),             # one resolved site: 101
    ("Synthetic Co", "102", "100000004", []),            # the reviewer's pick, stored on the job
    ("SYN001RA4AED", None, None, ["Ambiguous"]),          # two sites: unlimited, both parents
])
def test_a_job_searches_at_the_invoice_supplier_site(tmp_path, monkeypatch, store, supplier_name, supplier_site,
                                                     item_id, held):
    installed(store)
    result, trace = job(tmp_path, monkeypatch, store, [{**LINE, "sku": "VPN-4"}], supplier_name, supplier_site)
    assert result["invoice"]["lines"][0].get("item_id") == item_id
    assert [h["status"] for h in trace["held"]] == held


def test_a_pick_through_the_real_job_child_adds_a_rule_that_fills_this_invoice_and_the_next(tmp_path, monkeypatch, store):
    """The AI fallback's whole path with the child running: context, pick, rule, marked as AI, counted on the rule."""
    installed(store)
    line = {"sku": "PRINTED-77", "description": "Rose hand cream synthetic 50 ml", "qty": "2", "price": "5.00", "net_amount": "10.00"}
    read = {"text": "INVOICE SYN-1\nTotal 10.00\n", "boxes": [], "tables": [], "extraction_method": "template",
            "invoice": {"number": "SYN-1", "supplier_name": "Synthetic Co", "currency": "AED", "date": "2026-10-01",
                        "net": "10.00", "tax": "0.50", "lines": [line]}}
    monkeypatch.setattr(engines, "local_read", lambda *a, **k: json.loads(json.dumps(read)))
    monkeypatch.setattr(engines, "capabilities", lambda: [
        {"id": e, "installed": e == "invoice2data", "models_blocked": False, "version": None, "note": ""}
        for e in ("invoice2data", "paddleocr", "docling")])
    asked = []

    def foundry(path, text, options, examples=None, context=None):
        asked.append(context)
        wanted = next(c for c in context["candidates"] if c["fields"].get("item_id") == "100000001")
        suggestions, _ = ai_context.accept(context, {"suggestions": [
            {"candidate_id": wanted["id"], "field": "item_id", "why": "same item"}]})
        return Invoice.model_validate(read["invoice"]), {"evidence": {"header": {}}, "suggestions": suggestions}

    options = ProcessingOptions(engine="auto", ai_fallback=True, provider="azure", model="agent")
    first = engines.process_job(ROOT / "samples" / "invoice.pdf", options, store, foundry, filename="SYN-1.pdf")
    assert len(asked) == 1 and asked[0]["rules"] and asked[0]["candidates"]
    ai = first["readers"]["ai"]
    assert len(ai["rules_added"]) == 1 and first["invoice"]["lines"][0]["item_id"] == "100000001"
    assert first["readers"]["lines"][0]["item_id"] == "ai" and ai["rules_applied"][0]["rule"] == ai["rules_added"][0]
    rule = ai_rules.all_rules(store)[0]
    assert (rule["kind"], rule["printed"], rule["item_id"], rule["lines_filled"]) == ("sku", "printed77", "100000001", 1)
    second = engines.process_job(ROOT / "samples" / "invoice.pdf", options, store, foundry, filename="SYN-2.pdf")
    assert second["invoice"]["lines"][0]["item_id"] == "100000001" and second["readers"]["lines"][0]["item_id"] == "ai"
    assert ai_rules.all_rules(store)[0]["lines_filled"] == 2
    assert len(asked) == 1 and second["readers"]["ai"]["need"] == "cross_check"  # the rule answered; no second call
    removed = ai_rules.remove(store, rule["id"])
    assert removed and ai_rules.all_rules(store) == []


def test_a_pick_on_a_read_that_fails_its_checks_adds_no_rule_through_the_real_job_child(tmp_path, monkeypatch, store):
    installed(store)
    line = {"sku": "PRINTED-77", "description": "Rose hand cream synthetic 50 ml", "qty": "2", "price": "5.00", "net_amount": "10.00"}
    read = {"text": "INVOICE SYN-1\nTotal 10.00\n", "boxes": [], "tables": [], "extraction_method": "ocr",
            "invoice": {"number": "SYN-1", "supplier_name": "Synthetic Co", "currency": "AED", "date": "2026-10-01",
                        "net": "99.00", "tax": "0.50", "lines": [line]}}
    monkeypatch.setattr(engines, "local_read", lambda *a, **k: json.loads(json.dumps(read)))
    monkeypatch.setattr(engines, "capabilities", lambda: [
        {"id": e, "installed": e in ("invoice2data", "paddleocr"), "models_blocked": False, "version": None, "note": ""}
        for e in ("invoice2data", "paddleocr", "docling")])
    asked = []

    def foundry(path, text, options, examples=None, context=None):
        asked.append(context)
        wanted = next(c for c in context["candidates"] if c["fields"].get("item_id") == "100000001")
        suggestions, _ = ai_context.accept(context, {"suggestions": [
            {"candidate_id": wanted["id"], "field": "item_id", "why": "same item"}]})
        return Invoice.model_validate({**read["invoice"], "lines": []}), {"evidence": {"header": {}}, "suggestions": suggestions}

    options = ProcessingOptions(engine="auto", ai_fallback=True, provider="azure", model="agent")
    result = engines.process_job(ROOT / "samples" / "invoice-scan.png", options, store, foundry, filename="SYN-1.png")
    assert len(asked) == 1 and result["readers"]["ai"]["status"] == "not_read"
    assert ai_rules.all_rules(store) == [] and not result["readers"]["ai"].get("rules_added")
