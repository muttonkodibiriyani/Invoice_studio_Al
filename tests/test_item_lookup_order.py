"""Tharak's item lookup (5 Nov): VPN, from the VPN column or a code at the start or end of the description (D7),
then barcode, then a description match. Synthetic look-alikes of real description shapes only."""
from decimal import Decimal

import pytest

from app.fine_rules import RowsSource, run_invoice, vpn_candidates
from app.models import Invoice, Line


def item(parent, vpn, site="101", barcode=None, desc="Synthetic cream"):
    return {"ITEM_PARENT": parent, "VPN": vpn, "SUPPLIER SITE": site, "SUPPLIER_NAME": "ABC001RE4AED",
            "SUPPLIER SITE NAME": "Synthetic Co", "BARCODE": barcode, "ITEM_DESC": desc}


def first_line(rows, **line):
    inv = Invoice(number="INV-1", supplier_name="Synthetic Co", currency="AED", net=Decimal(10), tax=Decimal(0),
                  lines=[Line(qty=1, price=Decimal(10), net_amount=Decimal(10), **line)])
    result = run_invoice(inv, RowsSource(rows), text_value="Synthetic Co\nInvoice INV-1")
    return result["lines"][0]["Item"], result


@pytest.mark.parametrize("description, values", [
    ("SYNTHBRAND- Rose Cream 50ml SYN4417", ["SYN4417"]),
    ("SYN4417 Rose Cream SYN9921", ["SYN4417", "SYN9921"]),
    ("SYNTHBRAND- Rose Cream 100ML", ["100ML"]),
    ("SYNTHBRAND- Rose Cream ROSE", []),
    ("SYN4417", ["SYN4417"]),
    ("SYNTHBRAND- Rose Cream SYN4417.", ["SYN4417"]),
])
def test_a_code_with_a_digit_at_the_end_of_the_description_is_tried_after_the_leading_one(description, values):
    """D7: the trailing code is a candidate only when it holds a digit, after the leading code."""
    assert [c["value"] for c in vpn_candidates(Line(description=description))] == values


def test_a_trailing_code_finds_the_item_on_an_exact_vpn_in_the_supplier_site():
    found, result = first_line([item("P1", "SYN4417")], description="SYNTHBRAND- Rose Cream 50ml SYN4417")
    assert found == "P1"
    assert result["lines"][0]["VPN"] == "SYN4417"


def test_a_trailing_size_that_is_no_vpn_changes_nothing():
    found, result = first_line([item("P1", "SYN4417")], description="SYNTHBRAND- Rose Cream 100ML")
    assert found == ""
    assert not any("VPN" in e["Description"] for e in result["exceptions"] if e["Rule ID"] == "ALG-020")


def test_a_trailing_code_outside_the_supplier_site_is_not_accepted():
    rows = [item("P1", "SYN0001"), item("P2", "SYN4417", site="999")]
    rows[1]["SUPPLIER SITE NAME"] = "Other Co"
    assert first_line(rows, description="SYNTHBRAND- Rose Cream SYN4417")[0] == ""


def test_the_leading_code_wins_and_the_trailing_one_is_not_tried():
    """D7: tried after the leading one; a leading hit is the VPN even when the trailing code names another item."""
    found, _ = first_line([item("P1", "SYN1111"), item("P2", "SYN2222")], description="SYN1111 Rose Cream SYN2222")
    assert found == "P1"


@pytest.mark.parametrize("master_vpn", ["SYN482915", "SYN482915S1", "syn482915"])
def test_a_master_vpn_containing_the_invoice_vpn_is_the_item(master_vpn):
    """D9 (Tharak, 5 Nov): the master adds a three-letter brand prefix and sometimes an 'S1' suffix."""
    found, result = first_line([item("P1", master_vpn), item("P2", "SYN100001")], sku="482915")
    assert found == "P1"
    assert result["lines"][0]["VPN"] == master_vpn


def test_a_contained_vpn_is_tried_on_a_leading_description_code_too():
    assert first_line([item("P1", "SYN482915")], description="482915 SYNTHBRAND- Rose Cream 50ml")[0] == "P1"


def test_brand_prefix_and_s1_rows_of_one_parent_are_one_item():
    rows = [item("P1", "SYN482915"), item("P1", "SYN482915S1")]
    assert first_line(rows, sku="482915")[0] == "P1"


def test_a_contained_vpn_on_two_parents_is_not_accepted():
    found, result = first_line([item("P1", "SYN482915"), item("P2", "ABC482915S1")], sku="482915")
    assert found == ""


@pytest.mark.parametrize("sku", ["4829", "SYNTH", "ROSECREAM"])
def test_a_short_or_digitless_invoice_vpn_is_not_searched_inside_master_vpns(sku):
    assert first_line([item("P1", f"XYZ{sku}12")], sku=sku)[0] == ""


def test_a_contained_vpn_outside_the_supplier_site_is_ignored():
    rows = [item("P1", "SYN100001"), item("P2", "SYN482915", site="999")]
    rows[1]["SUPPLIER SITE NAME"] = "Other Co"
    assert first_line(rows, sku="482915")[0] == ""


def test_an_exact_vpn_wins_over_a_contained_one():
    assert first_line([item("P1", "482915"), item("P2", "SYN482915")], sku="482915")[0] == "P1"


# Tharak's lookup order: VPN (column, description start/end code, contained VPN) -> barcode -> description match.
BARCODE = "4006381333931"


def test_lookup_order_vpn_before_barcode_and_a_disagreement_is_held():
    rows = [item("P1", "SYN482915"), item("P2", "SYN100001", barcode=BARCODE)]
    found, result = first_line(rows, sku="482915", gtin=BARCODE)
    assert found == "" and any(e["Rule ID"] == "ALG-020" for e in result["exceptions"])


def test_lookup_order_barcode_when_no_vpn_matches():
    rows = [item("P1", "SYN482915"), item("P2", "SYN100001", barcode=BARCODE)]
    assert first_line(rows, sku="777777", gtin=BARCODE)[0] == "P2"


def test_lookup_order_description_last_and_only_as_candidates():
    found, result = first_line([item("P1", "SYN482915", desc="Synthetic rose cream")], sku="777777",
                               description="Synthetic rose cream")
    assert found == ""
    assert result["lines"][0]["Item"] == "" and any(e["Rule ID"] == "ALG-019" for e in result["exceptions"])
