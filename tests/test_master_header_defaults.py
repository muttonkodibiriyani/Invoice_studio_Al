"""Item master rules v0.3 for invoices with no accepted order: sites of one EBS supplier (D1), the empty location
type (L4.2, A6) and tax by the invoice's country (T5.1, A7)."""
from decimal import Decimal

from app import target_check as tc
from app.fine_rules import STORE, WAREHOUSE, RowsSource, RulesConfig, run_invoice
from app.matching import rules_view
from app.models import Invoice, Line

CONFIG = RulesConfig.from_dict({"version": "t", "vat_codes": [
    {"region": "ARE", "code": "UAEV5", "rate": "5", "active_from": "2020-01-01"}]})
A, B = "4006381333931", "5901234123457"


def item(parent, site, name, barcode):
    return {"ITEM_PARENT": parent, "SUPPLIER SITE": site, "SUPPLIER_NAME": name, "SUPPLIER SITE NAME": "Lux Co",
            "BARCODE": barcode, "ITEM_DESC": f"item {parent}"}


def run(rows, currency="AED", tax="10.00"):
    lines = [Line(gtin=A, description="item P1", qty=1, price=Decimal(100), net_amount=Decimal(100)),
             Line(gtin=B, description="item P2", qty=1, price=Decimal(100), net_amount=Decimal(100))]
    inv = Invoice(number="INV-1", supplier_name="Lux Co", date="2026-08-13", currency=currency, net=Decimal(200),
                  tax=Decimal(tax), lines=lines)
    return run_invoice(inv, RowsSource(rows), CONFIG, text_value="Lux Co\nInvoice INV-1\nitem P1\nitem P2")


def test_the_re4_site_of_an_ebs_supplier_is_chosen():
    """D1 (Tharak, 5 Nov): the name's sites share one EBS code; the RE4 site is the site."""
    result = run([item("P1", "S1", "LUX009RE4AED", A), item("P1", "S2", "LUX009RD3AED", A),
                  item("P2", "S1", "LUX009RE4AED", B)])
    assert result["header"]["Supplier Site"] == "S1"
    assert [line["Item"] for line in result["lines"]] == ["P1", "P2"]


def test_the_re4_site_is_chosen_even_when_every_site_holds_the_items():
    """D1 replaces A2's 'one site holding every item': both sites hold P1 and P2, the RE4 site still wins."""
    result = run([item(p, s, n, b) for p, b in (("P1", A), ("P2", B))
                  for s, n in (("S2", "LUX009RD3AED"), ("S1", "LUX009RE4AED"))])
    assert result["header"]["Supplier Site"] == "S1"
    assert [line["Item"] for line in result["lines"]] == ["P1", "P2"]


def test_the_re4_site_is_chosen_when_no_item_is_found():
    """D1: the RE4 site does not wait for matched items."""
    other = "0000000000000"
    result = run([item("P9", "S1", "LUX009RE4AED", other), item("P9", "S2", "LUX009RD3AED", other)])
    assert result["header"]["Supplier Site"] == "S1"


def test_without_an_re4_site_the_reviewer_picks():
    """D1: no RE4 site among several sites of one EBS supplier leaves the pick to the reviewer (S1.2)."""
    result = run([item(p, s, n, b) for p, b in (("P1", A), ("P2", B))
                  for s, n in (("S1", "LUX009RD4AED"), ("S2", "LUX009RD3AED"))])
    assert result["header"]["Supplier Site"] == ""
    assert [line["Item"] for line in result["lines"]] == ["", ""]
    assert [c["supplier_site"] for c in result["supplier_site_candidates"]] == ["S1", "S2"]


def test_sites_of_different_ebs_suppliers_stay_unresolved():
    """Different six-character codes are different suppliers: nothing is searched."""
    result = run([item("P1", "S1", "LUX009RE4AED", A), item("P2", "S2", "ABC001RD3AED", B)])
    assert result["header"]["Supplier Site"] == ""
    assert [line["Item"] for line in result["lines"]] == ["", ""]


def test_an_unknown_location_is_a_store_and_tax_follows_the_invoice_currency():
    """A6 + A7 (assumed, doc 01a1a88a-0301): L4.2 empty location is Store (S); T5.1 AED is UAE, UAEV5."""
    result = run([item("P1", "S1", "LUX009RE4AED", A), item("P2", "S1", "LUX009RE4AED", B)])
    assert (result["header"]["Location"], result["header"]["Location Type"]) == ("", STORE)
    assert [line["Unit Tax Code"] for line in result["lines"]] == ["UAEV5", "UAEV5"]
    assert result["status"] != "Approved"


def test_no_tax_code_without_a_known_country_or_with_a_wrong_rate():
    """A7 (assumed): no country for the currency, or a rate that disagrees, leaves the tax code blank."""
    rows = [item("P1", "S1", "LUX009RE4AED", A), item("P2", "S1", "LUX009RE4AED", B)]
    assert {line["Unit Tax Code"] for line in run(rows, currency="KWD")["lines"]} == {""}
    assert {line["Unit Tax Code"] for line in run(rows, tax="30.00")["lines"]} == {""}


def location_type_cell(view, **config):
    sources = tc.Sources(config=config, text="Lux Co\nInvoice INV-1\nitem P1\nitem P2")
    return next(c for c in tc.check_view(view, sources)["cells"] if c["column"] == "Location Type")


def unknown_location_view():
    return rules_view(run([item("P1", "S1", "LUX009RE4AED", A), item("P2", "S1", "LUX009RE4AED", B)]), "", [])


def test_the_store_of_an_unknown_location_is_rule_derived_not_a_mismatch():
    """L4.2 (D6): nothing is printed to find; the cell says it comes from the owner rule."""
    cell = location_type_cell(unknown_location_view())
    assert (cell["status"], cell["sub"], cell["group"]) == (tc.VERIFIED, tc.RULE_DERIVED, tc.VERIFIED)
    assert "L4.2" in cell["reason"] and cell["evidence"]["rule"] == "L4.2"


def d6(view, value=None, rule=None, location=None):
    """The unknown-location view with one thing about the D6 cell changed."""
    fields = view["fields"]
    cell = fields["location_type"]
    evidence = {**cell["evidence"][0], "rule": rule or cell["evidence"][0]["rule"]}
    return {**view, "fields": {**fields, "location_type": {**cell, "value": value or cell["value"], "evidence": [evidence]},
                               "location": {**fields["location"], "value": location or ""}}}


def test_the_d6_source_proves_nothing_but_a_store_at_an_unknown_location():
    view = unknown_location_view()
    for changed in (d6(view, value=WAREHOUSE), d6(view, value="Kiosk"), d6(view, rule="ALG-013"),
                    d6(view, location="1234")):
        cell = location_type_cell(changed)
        assert (cell["status"], cell["sub"]) == (tc.MISMATCH, ""), changed["fields"]["location_type"]
        assert cell["reason"] == "Evidence does not hold the value"


def test_the_line_amounts_total_reaches_the_view_only_when_every_line_prints_one():
    rows = [item("P1", "S1", "LUX009RE4AED", A), item("P2", "S1", "LUX009RE4AED", B)]
    assert unknown_location_view()["line_amounts_total"] == "200"
    lines = [Line(gtin=A, description="item P1", qty=1, price=Decimal(100), net_amount=Decimal(100)),
             Line(gtin=B, description="item P2", qty=1, price=Decimal(100))]
    inv = Invoice(number="INV-1", supplier_name="Lux Co", date="2026-08-13", currency="AED", net=Decimal(200),
                  tax=Decimal(10), lines=lines)
    result = run_invoice(inv, RowsSource(rows), CONFIG, text_value="Lux Co\nInvoice INV-1\nitem P1\nitem P2")
    assert "line_amounts_total" not in rules_view(result, "", [])
