"""Location Type is written Store (S) / Warehouse (W), normalised from the item master (rule L4.2).

The variants are the shipped rules workbook's Location_Type_Variants sheet. The workbook writes
'Store(S)'; the output keeps the target template's 'Store (S)' (coordinator decision, 4 Nov).
Location is the matched row's LOCATION NUMBER (rule L4.1); blank means the user adds it (P-LOC)."""
import io

import pytest
from openpyxl import load_workbook

from app import location_type as lt
from app.item_rules import rules

VARIANTS = rules().location_type_variants()
S, W = "Store (S)", "Warehouse (W)"


@pytest.mark.parametrize("raw, written", [
    (None, S), ("", S), ("   ", S),
    ("STORE", S), ("Store", S), ("store", S), (" Store ", S),
    ("Store(S)", S), ("Store (S)", S), ("store(s)", S), ("STORE ( S )", S),
    ("S", S), ("s", S), (" s ", S),
    ("WAREHOUSE", W), ("Warehouse", W), ("warehouse", W),
    ("Warehouse(W)", W), ("Warehouse (W)", W), ("warehouse (w)", W),
    ("W", W), ("w", W),
    ("Depot", None), ("Shop", None), ("WH", None), ("Store/Warehouse", None),
    ("(anything else)", None), ("(empty)", None),
])
def test_variant_is_normalised_or_held(raw, written):
    assert lt.normalise(raw, VARIANTS) == written


def test_a_new_spelling_is_a_new_workbook_row_not_a_code_change():
    assert lt.normalise("Shop", VARIANTS) is None
    assert lt.normalise("Shop", {**VARIANTS, "Shop": "Store(S)"}) == S


def test_a_variant_row_naming_an_unknown_target_is_ignored():
    assert lt.normalise("Depot", {**VARIANTS, "Depot": "Depot (D)"}) is None


@pytest.mark.parametrize("value, written", [
    ("Store(S)", S), ("Store (S)", S), ("store (s)", S),
    ("Warehouse(W)", W), ("Warehouse (W)", W), ("WAREHOUSE(W)", W),
    ("Store", None), ("S", None), ("", None), (None, None), ("Depot", None),
])
def test_written_form_accepts_only_the_two_types_with_or_without_a_space(value, written):
    assert lt.written(value) == written


def test_resolve_takes_location_and_type_from_the_matched_row():
    row = {"LOCATION NUMBER": "1200", "LOCATION NAME": "Warehouse", "LOCATION NAME 2": ""}
    assert lt.resolve(row, VARIANTS) == lt.Location("1200", W, ())


def test_resolve_writes_store_when_the_type_is_blank():
    assert lt.resolve({"LOCATION NUMBER": 1200}, VARIANTS) == lt.Location("1200", S, ())


def test_resolve_falls_back_to_location_name_2():
    row = {"LOCATION NUMBER": "1200", "LOCATION NAME": "", "LOCATION NAME 2": "W"}
    assert lt.resolve(row, VARIANTS).type == W


@pytest.mark.parametrize("number", [None, "", "  "])
def test_blank_location_number_asks_the_user_to_add_it(number):
    result = lt.resolve({"LOCATION NUMBER": number, "LOCATION NAME": "Store"}, VARIANTS)
    assert result == lt.Location(None, S, (lt.ASK_LOCATION,))


def test_unknown_type_is_held_for_the_user_to_pick():
    result = lt.resolve({"LOCATION NUMBER": "1200", "LOCATION NAME": "Depot"}, VARIANTS)
    assert result == lt.Location("1200", None, (lt.ASK_LOCATION_TYPE,))


def test_export_writes_the_template_text_for_a_no_space_value():
    from app.excel import HEADERS, batch_workbook

    class Invoice:
        location, number, site, po, date, net, tax, taxCode = "1200", "INV-1", "101", "450001", "2026-11-01", 100, 5, "V5"
        lines = ()

    book = load_workbook(io.BytesIO(batch_workbook([(Invoice(), {"ready": True, "location_type": "store(s)",
                                                                  "matches": []})])))
    header = book["Header"]
    assert header.cell(2, HEADERS["Header"].index("Location Type") + 1).value == S
    assert [dv.formula1 for dv in header.data_validations.dataValidation] == [f'"{S},{W}"']


def test_target_check_reads_a_no_space_value_as_the_template_text():
    from app import target_check as tc
    from tests.test_target_check import view
    sheet = view()
    sheet["fields"]["location_type"] = {"value": "Warehouse(W)"}
    assert tc.planned_rows(sheet)["Header"][1][5] == W
