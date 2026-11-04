"""The rules workbook is the matcher's rule source: order, variants, tiers, tax codes and prompts are rows, not code."""
import hashlib
from decimal import Decimal

import openpyxl
import pytest

from app import item_rules
from app.item_rules import RulesError, load

LOOKUP_HEADERS = ["Rule ID", "Step", "Order in step", "Rule", "Invoice field (input)", "Item master column searched",
                  "Match type", "Search limited to", "Gives (writes)", "Target column(s)", "If exactly one match",
                  "If no match", "If more than one match", "Reviewer sees (short)", "Status", "Enabled"]


def rule(rule_id, step, order, column, match, one="Found", many="Ambiguous", prompt="P-X", enabled="Y"):
    return [rule_id, step, order, "rule", "input", column, match, "scope", "gives", "target", one, "next", many,
            prompt, "Confirmed", enabled]


def write_workbook(path, lookup=None, variants=None, tiers=None, taxes=None, prompts=None, version="v9.1, test"):
    wb = openpyxl.Workbook()
    wb.active.title = "README"
    wb["README"].append(["Title", None])
    wb["README"].append(["Version", version])
    sheet = wb.create_sheet("Lookup_Sequence")
    sheet.append(LOOKUP_HEADERS)
    for row in lookup if lookup is not None else [
            rule("I9.2", "2 Item (per line)", 2, "BARCODE (then BARCODE_1)", "Exact (digits only)"),
            rule("I9.1", "2 Item (per line)", 1, "VPN", "Exact (trimmed)"),
            rule("I9.3", "2 Item (per line)", 3, "ITEM_DESC", "Normalised fuzzy match, top 3 above the threshold",
                 one="Suggestion: the user confirms it", prompt="P-Y"),
            rule("I9.9", "2 Item (per line)", 4, "QTY_RECEIVED", "Exact number", enabled="N"),
            rule("I9.5", "2 Item (per line)", 5, "-", "-", prompt="P-Y"),
            rule("S9.1", "1 Supplier", 1, "SUPPLIER SITE NAME", "Exact", prompt="P-X / P-Y")]:
        sheet.append(row)
    sheet = wb.create_sheet("Location_Type_Variants")
    sheet.append(["Item master value", "Written as", "Notes"])
    for row in variants if variants is not None else [
            ["(empty)", "Store(S)"], ["Store (S)", "Store(S)"], ["W", "Warehouse(W)"], ["(anything else)", "Held"]]:
        sheet.append(row)
    sheet = wb.create_sheet("Coverage_Tiers")
    sheet.append(["Tier", "From % (inclusive)", "To % (exclusive)", "What happens", "Export", "Reviewer sees"])
    for row in tiers if tiers is not None else [
            ["Low", 0, 70, "ask", "N", "P-Y"], ["Top", 100, None, "export", "Y", "-"], ["Mid", 70, 100, "fill", "After the user fills them", "P-X"],
            ["Coverage = a note row", None, None, None, None, None]]:
        sheet.append(row)
    sheet = wb.create_sheet("Tax_Codes")
    sheet.append(["Country", "Market", "VAT rate %", "Tax Code", "Valid from", "Status", "Notes"])
    for row in taxes if taxes is not None else [["Atlantis", "ATL", 7, "ATLV7", None, "Assumed", None]]:
        sheet.append(row)
    sheet = wb.create_sheet("Reviewer_Prompts")
    sheet.append(["Prompt ID", "When", "Message (keep it short)", "Buttons"])
    for row in prompts if prompts is not None else [
            ["P-X", "x happens", "Line {n}: {vpn}?", "Yes · Pick another"], ["P-Y", "y happens", "Nothing.", "OK"]]:
        sheet.append(row)
    wb.save(path)
    path.with_name(path.name + ".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")
    return path


@pytest.fixture
def workbook(tmp_path):
    return write_workbook(tmp_path / "rules.xlsx")


def test_shipped_workbook_loads_with_its_checksum_and_every_prompt_it_names():
    rules = load(item_rules.DEFAULT_WORKBOOK)
    assert rules.version.startswith("v")
    assert rules.sha256 == hashlib.sha256(item_rules.DEFAULT_WORKBOOK.read_bytes()).hexdigest()
    items = rules.lookup_order(2)
    assert items and items[-1].terminal and not any(r.terminal for r in items[:-1])
    assert {p for r in rules.lookup_order() for p in r.prompts} <= set(rules.prompts())


def test_lookup_order_is_step_then_order_and_skips_disabled_rows(workbook):
    rules = load(workbook)
    assert [r.id for r in rules.lookup_order()] == ["S9.1", "I9.1", "I9.2", "I9.3", "I9.5"]
    items = rules.lookup_order(2)
    assert [r.id for r in items] == ["I9.1", "I9.2", "I9.3", "I9.5"]
    vpn, barcode, desc, none = items
    assert (vpn.columns, vpn.match, vpn.confirm, vpn.limit) == (("VPN",), "exact", False, None)
    assert barcode.columns == ("BARCODE", "BARCODE_1")
    assert (desc.columns, desc.match, desc.confirm, desc.limit) == (("ITEM_DESC",), "fuzzy", True, 3)
    assert none.terminal and (none.columns, none.match) == ((), "")
    assert rules.lookup_order(1)[0].prompts == ("P-X", "P-Y")


def test_a_new_row_is_a_new_rule_without_code(tmp_path):
    rows = [rule("I9.1", "2 Item", 1, "VPN", "Exact"), rule("I9.2", "2 Item", 2, "BARCODE_1", "Exact"),
            rule("I9.4", "2 Item", 3, "QTY_RECEIVED", "Exact number. Blank rows are skipped",
                 one="Suggestion: the user confirms it", many="Show the candidate rows (max 5)")]
    qty = load(write_workbook(tmp_path / "r.xlsx", lookup=rows)).lookup_order(2)[-1]
    assert (qty.columns, qty.match, qty.confirm, qty.limit) == (("QTY_RECEIVED",), "number", True, 5)


def test_location_type_variants_keep_the_written_form_and_leave_unknowns_out(workbook):
    assert load(workbook).location_type_variants() == {"": "Store(S)", "Store (S)": "Store(S)", "W": "Warehouse(W)"}


def test_coverage_tiers_are_highest_first_with_open_top(workbook):
    tiers = load(workbook).coverage_tiers()
    assert [(t.name, t.low, t.high, t.export, t.prompt) for t in tiers] == [
        ("Top", Decimal(100), None, "Y", None), ("Mid", Decimal(70), Decimal(100), "After the user fills them", "P-X"),
        ("Low", Decimal(0), Decimal(70), "N", "P-Y")]


def test_coverage_export_mode_is_normalised_from_the_sheet_text(workbook):
    assert [t.export_mode for t in load(workbook).coverage_tiers()] == ["yes", "after_fill", "no"]
    assert [t.export_mode for t in load(item_rules.DEFAULT_WORKBOOK).coverage_tiers()] == [
        "yes", "after_fill", "after_fill", "no"]


@pytest.mark.parametrize("country", ["Atlantis", "atlantis ", "ATL", "atl"])
def test_tax_code_by_country_or_market(workbook, country):
    assert load(workbook).tax_code(country) == "ATLV7"


@pytest.mark.parametrize("country", ["", None, "Elsewhere"])
def test_unknown_country_has_no_tax_code(workbook, country):
    assert load(workbook).tax_code(country) is None


def test_prompts_by_id(workbook):
    prompt = load(workbook).prompts()["P-X"]
    assert (prompt.message, prompt.buttons) == ("Line {n}: {vpn}?", ("Yes", "Pick another"))
    assert prompt.message.format(n=2, vpn="A1") == "Line 2: A1?"


def test_a_changed_workbook_is_refused(workbook):
    workbook.write_bytes(workbook.read_bytes() + b"\0")
    with pytest.raises(RulesError, match="checksum"):
        load(workbook)


def test_a_workbook_without_a_checksum_is_refused(workbook):
    workbook.with_name(workbook.name + ".sha256").unlink()
    with pytest.raises(RulesError, match="checksum"):
        load(workbook)


def test_env_names_the_workbook_and_its_checksum(workbook, monkeypatch):
    digest = workbook.with_name(workbook.name + ".sha256").read_text().strip()
    workbook.with_name(workbook.name + ".sha256").unlink()
    monkeypatch.setenv("INV_STUDIO_RULES_WORKBOOK", str(workbook))
    monkeypatch.setenv("INV_STUDIO_RULES_SHA256", digest)
    item_rules.rules.cache_clear()
    try:
        assert item_rules.rules().sha256 == digest
    finally:
        item_rules.rules.cache_clear()


@pytest.mark.parametrize("kwargs, message", [
    ({"lookup": [rule("I9.1", "2 Item", 1, "VPN", "Exact"), rule("I9.1", "2 Item", 2, "BARCODE", "Exact")]},
     "I9.1.*more than once"),
    ({"lookup": [rule("I9.1", "2 Item", 1, "VPN", "Exact", prompt="P-NOPE")]}, "P-NOPE"),
    ({"tiers": [["Top", 100, None, "export", "Y", "-"], ["Low", 0, 90, "ask", "N", "-"]]}, "90"),
    ({"tiers": [["Top", 100, None, "export", "Maybe", "-"], ["Low", 0, 100, "ask", "N", "-"]]}, "Maybe"),
    ({"taxes": [["Atlantis", "ATL", 7, "", None, "Assumed", None]]}, "Atlantis"),
])
def test_an_inconsistent_workbook_is_refused(tmp_path, kwargs, message):
    with pytest.raises(RulesError, match=message):
        load(write_workbook(tmp_path / "bad.xlsx", **kwargs))


def test_a_missing_sheet_is_refused(workbook):
    wb = openpyxl.load_workbook(workbook)
    del wb["Tax_Codes"]
    wb.save(workbook)
    workbook.with_name(workbook.name + ".sha256").write_text(hashlib.sha256(workbook.read_bytes()).hexdigest())
    with pytest.raises(RulesError, match="Tax_Codes"):
        load(workbook)


# ------------------------------------------------------------------ app.matching.lookup_item: the item step in workbook order
from app.matching import lookup_item  # noqa: E402


def item_rules_wb(tmp_path, *extra):
    rows = [rule("I9.1", "2 Item", 1, "VPN", "Exact", many="Narrow, else Ambiguous", prompt="P-X"),
            rule("I9.2", "2 Item", 2, "BARCODE (then BARCODE_1)", "Exact (digits only)",
                 one="Found. If the VPN matched a different parent: Conflict (P-Y)", prompt="P-X"),
            rule("I9.3", "2 Item", 3, "ITEM_DESC", "Fuzzy, top 3", one="Suggestion: the user confirms it", prompt="P-X"),
            rule("I9.4", "2 Item", 4, "QTY_RECEIVED", "Exact number", one="Suggestion: the user confirms it",
                 many="Show the candidate rows (max 5)", prompt="P-X"),
            *extra,
            rule("I9.5", "2 Item", 9, "-", "-", prompt="P-Y")]
    return load(write_workbook(tmp_path / "items.xlsx", lookup=rows))


def searcher(hits):
    calls = []

    def search(column, match):
        calls.append((column, match))
        return hits.get(column, [])
    search.calls = calls
    return search


def test_vpn_found_first_and_barcode_only_cross_checks(tmp_path):
    search = searcher({"VPN": ["P1"], "BARCODE": ["P1"], "ITEM_DESC": ["P9"]})
    assert lookup_item(search, item_rules_wb(tmp_path)) == {
        "rule": "I9.1", "status": "Found", "parents": ["P1"], "confirm": False, "prompt": None}
    assert search.calls == [("VPN", "exact"), ("BARCODE", "exact")]


def test_exact_ids_pointing_at_different_parents_are_a_conflict(tmp_path):
    result = lookup_item(searcher({"VPN": ["P1"], "BARCODE": ["P2"]}), item_rules_wb(tmp_path))
    assert result == {"rule": "I9.2", "status": "Conflict", "parents": ["P1", "P2"], "confirm": False, "prompt": "P-Y"}


def test_a_second_exact_id_narrows_an_ambiguous_first(tmp_path):
    result = lookup_item(searcher({"VPN": ["P1", "P2"], "BARCODE": ["P2", "P3"]}), item_rules_wb(tmp_path))
    assert (result["rule"], result["status"], result["parents"]) == ("I9.1", "Found", ["P2"])


def test_an_ambiguous_id_asks_the_user_to_pick(tmp_path):
    result = lookup_item(searcher({"VPN": ["P1", "P2"]}), item_rules_wb(tmp_path))
    assert (result["status"], result["parents"], result["prompt"]) == ("Ambiguous", ["P1", "P2"], "P-X")


def test_barcode_falls_back_to_its_second_column(tmp_path):
    result = lookup_item(searcher({"BARCODE_1": ["P4"]}), item_rules_wb(tmp_path))
    assert (result["rule"], result["status"], result["parents"]) == ("I9.2", "Found", ["P4"])


def test_description_is_a_suggestion_the_user_confirms_capped_at_its_limit(tmp_path):
    result = lookup_item(searcher({"ITEM_DESC": ["P1", "P2", "P3", "P4"], "QTY_RECEIVED": ["P5"]}), item_rules_wb(tmp_path))
    assert result == {"rule": "I9.3", "status": "Suggestion", "parents": ["P1", "P2", "P3"], "confirm": True,
                      "prompt": "P-X"}


def test_quantity_is_the_last_suggestion(tmp_path):
    result = lookup_item(searcher({"QTY_RECEIVED": [f"P{i}" for i in range(9)]}), item_rules_wb(tmp_path))
    assert (result["rule"], result["status"], len(result["parents"])) == ("I9.4", "Suggestion", 5)


def test_nothing_found_ends_on_the_terminal_row(tmp_path):
    search = searcher({})
    assert lookup_item(search, item_rules_wb(tmp_path)) == {
        "rule": "I9.5", "status": "Not in item master", "parents": [], "confirm": False, "prompt": "P-Y"}
    assert [c for c, _ in search.calls] == ["VPN", "BARCODE", "BARCODE_1", "ITEM_DESC", "QTY_RECEIVED"]


def test_a_new_exact_row_is_searched_without_code(tmp_path):
    rules = item_rules_wb(tmp_path, rule("I9.6", "2 Item", 5, "RMS_ITEM_ID", "Exact", prompt="P-X"))
    result = lookup_item(searcher({"RMS_ITEM_ID": ["P7"]}), rules)
    assert (result["rule"], result["status"], result["parents"]) == ("I9.6", "Found", ["P7"])


def test_an_item_row_the_matcher_cannot_run_is_refused(tmp_path):
    rules = item_rules_wb(tmp_path, rule("I9.6", "2 Item", 5, "ITEM", "Sounds like", prompt="P-X"))
    with pytest.raises(RulesError, match="I9.6"):
        lookup_item(searcher({}), rules)


def test_the_shipped_item_order_is_vpn_barcode_description_quantity():
    seen = searcher({})
    assert lookup_item(seen, load(item_rules.DEFAULT_WORKBOOK))["status"] == "Not in item master"
    assert seen.calls == [("VPN", "exact"), ("BARCODE", "exact"), ("BARCODE_1", "exact"), ("ITEM_DESC", "fuzzy"),
                          ("QTY_RECEIVED", "number")]


def test_without_item_rows_nothing_is_found(tmp_path):
    rules = load(write_workbook(tmp_path / "none.xlsx", lookup=[rule("S9.1", "1 Supplier", 1, "SUPPLIER SITE NAME", "Exact")]))
    assert lookup_item(searcher({"VPN": ["P1"]}), rules) == {
        "rule": None, "status": "Not in item master", "parents": [], "confirm": False, "prompt": None}
