"""A printed unit and a reference unit compare by the FIELD_RULES vocabulary: spelling, never meaning."""
import json
import pathlib
import re
from decimal import Decimal

import pytest

from app import engines
from app.matching import validate
from app.models import UOM_SPELLINGS, Invoice, Policy, uom_code
from app.product_candidates import ProductCandidates
from app.reference_lookup import ReferenceLookup
from app.store import Store


@pytest.mark.parametrize("printed, code", [
    ("Units", "PCS"), ("unit", "PCS"), ("PCS", "PCS"), ("Pcs.", "PCS"), ("Nos", "PCS"), ("NO.", "PCS"), ("Pieces", "PCS"),
    ("each", "EA"), ("Boxes", "BOX"), ("Cartons", "CTN"), ("CS", "CTN"), ("Case", "CTN"), ("Kgs", "KG"), ("Pack", "PKT"),
    ("Litres", "L"), ("Pairs", "PR"), ("Dozen", "DZ"),
    # A unit outside the table stays itself, upper-cased as printed.
    ("Box of 12", "BOX OF 12"), ("Bundle", "BUNDLE"),
    (None, None), ("", None), ("  ", None), (".", None),
])
def test_a_printed_unit_maps_onto_the_vocabulary(printed, code):
    assert uom_code(printed) == code


def test_the_code_table_is_the_field_rules_table():
    rules = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "FIELD_RULES.md").read_text()
    table = rules.split("## Unit-of-measure vocabulary")[1].split("## ")[0]
    documented = {code: {spelling.strip().casefold() for spelling in spellings.split(",")}
                  for code, spellings in re.findall(r"^\| ([A-Z]+) \| (.+?) \|$", table, re.M)}
    assert documented == {code: set(spellings) for code, spellings in UOM_SPELLINGS.items()}


def test_each_and_pieces_stay_different_units():
    assert uom_code("EA") != uom_code("PCS")


REFS = {"sites": [{"id": "S1", "seller": "SEL", "name": "Synthetic Co"}], "routes": [],
        "items": [{"id": "I1", "site": "S1", "sku": "A1", "gtin": "", "uom": "PCS"}],
        "orders": [{"id": "PO1", "status": "open", "site": "S1", "buyer": "B", "location": "L", "currency": "AED",
                    "origin": "AE", "market": "AE", "location_type": "Store (S)",
                    "lines": [{"id": "1", "item": "I1", "ordered": "9", "invoiced": "0", "price": "5", "uom": "PCS"}]}],
        "receipts": []}


def uom_issues(uom):
    invoice = Invoice(number="N1", seller="SEL", site="S1", po="PO1", currency="AED", net=Decimal("10"),
                      lines=[{"sku": "A1", "qty": "2", "uom": uom, "price": "5"}])
    issues = validate(invoice, REFS, Policy())["issues"]
    return [issue for issue in issues if issue["code"] == "UOM"]


@pytest.mark.parametrize("uom", ["Units", "PCS", "pcs.", "Nos"])
def test_the_purchase_order_check_passes_the_same_unit_in_another_spelling(uom):
    assert uom_issues(uom) == []


@pytest.mark.parametrize("uom", ["EA", "CTN", None])
def test_the_purchase_order_check_still_fails_a_different_or_missing_unit(uom):
    assert len(uom_issues(uom)) == 1


def priced_search(tmp_path, uom):
    store = Store(tmp_path)
    lookup = ReferenceLookup(store)
    fields = {"sku": [], "gtin": [], "internal_item": [], "po": [], "description": ["Synthetic soap"], "uom": ["PCS"]}
    item = {"id": "item-1", "candidate_fields": fields}
    po_row = {"id": "po-1", "source_sheet": "PO", "source_row": 2, "flags": [], "candidate_fields": {},
              "data": {"UNIT_COST": "5", "CURRENCY_CODE": "AED", "RMS_ORDER_NO": "PO1"}}
    with store.connection(True) as c:
        c.execute("INSERT INTO lookup_sources(id,payload) VALUES('s','{}')")
        for row in (item, po_row):
            c.execute("INSERT INTO lookup_rows(id,kind,source_hash,sheet,row_number,payload) VALUES(?,?,?,?,?,?)",
                      (row["id"], "item" if row is item else "po", "s", "X", 1 if row is item else 2, json.dumps(row)))
        for kind, term, row_id in (("item", "n:synthetic soap", "item-1"), ("item", "x:key", "item-1"),
                                   ("po", "x:key", "po-1"), ("po", "f:po:po1", "po-1")):
            c.execute("INSERT INTO lookup_terms(kind,term,row_id) VALUES(?,?,?)", (kind, term, row_id))
    found = ProductCandidates(lookup).search("Synthetic soap", po="PO1", price="5", currency="AED", uom=uom)
    return found["records"][0]["match"]


def test_the_price_clue_scores_the_same_unit_in_another_spelling(tmp_path):
    match = priced_search(tmp_path, "Units")
    assert "po_price_similarity" in match["basis"]


def test_the_price_clue_stays_out_for_a_different_unit(tmp_path):
    match = priced_search(tmp_path, "EA")
    assert "po_price_similarity" not in match["basis"]


def test_two_readers_agree_on_the_same_unit_in_another_spelling():
    assert engines._same("uom", "Units", "PCS")
    assert not engines._same("uom", "EA", "PCS")
