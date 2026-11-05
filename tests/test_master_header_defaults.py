"""Item master rules v0.3 for invoices with no accepted order: sites of one EBS supplier (D1), the empty location
type (L4.2, A6) and tax by the invoice's country (T5.1, A7)."""
from decimal import Decimal

from app.fine_rules import STORE, RowsSource, RulesConfig, run_invoice
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
