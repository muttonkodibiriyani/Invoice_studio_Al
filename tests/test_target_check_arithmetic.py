"""A cell is 'verified' only when its row checks hold, not only because its value is printed.

Printed evidence proves a value was read from the page; it does not prove the page was read right. The
failure shapes come from a bulk run of real invoices (values here are synthetic): a single-digit tax read
for a hundreds-level VAT, the gross read into the net column, quantity x unit cost that is not the line
amount (a pre-discount price, a price read x100), and an 'Sl' document prefix (an 'SI' read with a
lower-case L). Each keeps its value and is held as needs_checking with the failed check named."""
from decimal import Decimal

import pytest

from app import target_check as tc

CONFIG = {"vat_codes": [{"region": "ARE", "code": "VATA5", "rate": "5", "active_from": "2020-01-01"}]}
VAT_SOURCE = "Owner VAT code table (C/PV), receiving market"


def field(value, original=None):
    return {"value": value, "evidence": [{"source": "Invoice page 1", "reference": "page 1 box 2",
                                          "original": original if original is not None else value}]}


def vat_field(code="VATA5"):
    return {"value": code, "evidence": [{"source": VAT_SOURCE, "reference": "", "original": code}]}


def line(number, qty, cost):
    return {"line": number, "cells": {"Item": field("123456789"), "Unit Cost": field(cost), "Quantity": field(qty),
                                      "Unit Tax Code": vat_field()}}


def view(number="DEMO-0543", net="100.00", tax="5.00", lines=None, code="VATA5", issues=()):
    return {"fields": {"number": field(number), "site": {"value": "", "reason": "Not found"},
                       "po": {"value": "", "reason": "Not found"}, "location": {"value": "", "reason": "Not found"},
                       "location_type": {"value": "", "reason": "Not found"}, "date": field("2031-03-03"),
                       "net": field(net), "tax": field(tax),
                       "taxCode": vat_field(code) if code else {"value": "", "reason": "Not found"},
                       "currency": {"value": "AED"}},
            "lines": lines if lines is not None else [line(1, "2", "50.00")], "issues": list(issues)}


def text(*rows):
    return "\n".join(("INVOICE DEMO-0543 SI0543 Sl0543 0543", "Date 2031-03-03", "Item 123456789", *rows))


def check(v, *rows):
    return tc.check_view(v, tc.Sources(config=CONFIG, text=text(*rows)))


def cell(result, sheet, column, line=None):
    return next(c for c in result["cells"] if (c["sheet"], c["column"], c["line"]) == (sheet, column, line))


def status_of(result, name):
    return next(c["status"] for c in result["checks"] if c["check"] == name)


AMOUNTS = (("Header", "Total Cost Ex Tax"), ("Header", "Tax Amount"), ("Tax_Breakdown", "Tax Basis"))


def test_a_consistent_invoice_stays_fully_verified():
    result = check(view(), "Widget 2 x 50.00 100.00", "Net 100.00", "VAT 5% 5.00", "Total 105.00")
    for key in (*AMOUNTS, ("Header", "Document"), ("Details", "Unit Cost", 1), ("Details", "Quantity", 1)):
        got = cell(result, *key)
        assert got["status"] == tc.VERIFIED, (key, got["reason"])
    assert not any(c["status"] == tc.CHECK_FAILED for c in result["cells"])
    assert result["failed"] == [] and not result["holds"]


def test_a_single_digit_tax_for_a_hundreds_level_vat_is_not_verified():
    # Net 2000.00 at 5 % is 100.00 VAT; the read took a stray 7.00. The rules blank the tax code on that
    # disagreement, so the rate comes from the currency's VAT region in the owner's VAT table.
    v = view(net="2000.00", tax="7.00", lines=[line(1, "4", "500.00")], code="")
    result = check(v, "Widget 4 x 500.00 2000.00", "Net 2000.00", "VAT 5% 100.00", "Pallets 7.00", "Total 2100.00")
    assert status_of(result, "tax_breakdown_to_header") == tc.FAIL
    assert status_of(result, "net_plus_tax_gross") == tc.WARNING
    tax = cell(result, "Header", "Tax Amount")
    assert tax["status"] == tc.CHECK_FAILED and tax["group"] == tc.NEEDS_CHECKING
    assert "tax_breakdown_to_header" in tax["reason"]
    assert cell(result, "Header", "Total Cost Ex Tax")["status"] == tc.CHECK_FAILED, "net + tax is not the gross"
    assert result["holds"] and "tax_breakdown_to_header" in result["failed"]


def test_the_tax_rate_is_never_assumed_without_the_owner_vat_table():
    v = view(code="")
    result = tc.check_view(v, tc.Sources(config={}, text=text("Widget 2 x 50.00", "Net 100.00 VAT 5.00 Total 105.00")))
    assert status_of(result, "tax_breakdown_to_header") == tc.SKIPPED
    assert cell(result, "Header", "Tax Amount")["status"] == tc.CHECK_FAILED
    assert cell(result, "Header", "Total Cost Ex Tax")["status"] == tc.VERIFIED, "net + tax = gross and lines = net"


def test_the_gross_read_into_the_net_column_is_not_verified():
    v = view(net="1050.00", tax="50.00", lines=[line(1, "10", "100.00")])
    result = check(v, "Widget 10 x 100.00 1000.00", "Net 1000.00", "VAT 5% 50.00", "Total 1050.00")
    assert status_of(result, "lines_to_net") == tc.FAIL
    for sheet, column in AMOUNTS:
        got = cell(result, sheet, column)
        assert got["status"] == tc.CHECK_FAILED, (sheet, column)
    assert "lines_to_net" in cell(result, "Header", "Total Cost Ex Tax")["reason"]
    assert result["holds"]


def test_lines_within_unit_cost_rounding_only_do_not_prove_the_net():
    # Whole-unit costs give a rounding bound of half a unit per item: 100 x 50 is 5000.00, a net 40.00 lower is
    # inside it (a warning), but nothing printed shows the lines make that net.
    v = view(net="4960.00", tax="248.00", lines=[line(1, "100", "50")])
    result = check(v, "Widget 100 x 50 4960.00", "Net 4960.00", "VAT 5% 248.00", "Total 5208.00")
    assert status_of(result, "lines_to_net") == tc.WARNING
    for sheet, column in AMOUNTS:
        got = cell(result, sheet, column)
        assert got["status"] == tc.CHECK_FAILED and "lines_to_net warning" in got["reason"], (sheet, column)


def test_lines_that_make_the_net_after_the_printed_discount_keep_it_verified():
    v = view(net="900.00", tax="45.00", lines=[line(1, "2", "500.00")])
    result = check(v, "Widget 2 x 500.00 1000.00", "Subtotal 1000.00", "Discount 100.00", "Net 900.00",
                   "VAT 5% 45.00", "Total 945.00")
    assert status_of(result, "lines_to_net") == tc.WARNING
    for sheet, column in AMOUNTS:
        assert cell(result, sheet, column)["status"] == tc.VERIFIED, (sheet, column)


def test_a_line_whose_quantity_times_cost_is_not_its_amount_is_not_verified():
    # Line 2 carries a pre-discount price, line 3 a price read x100; the rules raised a Line Parsing exception
    # (02A) on each. Line 1 adds up and stays verified.
    issues = [{"code": "Line Parsing Exception", "line": n, "rule": "02A-Unit Cost", "blocking": True,
               "check": "C-10", "type": "Line Exception"} for n in (2, 3)]
    lines = [line(1, "2", "50.00"), line(2, "1", "40.00"), line(3, "1", "1000.00")]
    v = view(net="110.00", tax="5.50", lines=lines, issues=issues)
    result = check(v, "A 2 x 50.00 100.00", "B 1 x 40.00 36.00", "C 1 x 1000.00 10.00", "Net 110.00",
                   "VAT 5.50", "Total 115.50")
    for n in (2, 3):
        for column in ("Unit Cost", "Quantity"):
            got = cell(result, "Details", column, n)
            assert got["status"] == tc.CHECK_FAILED and got["group"] == tc.NEEDS_CHECKING, (n, column)
            assert "line_arithmetic" in got["reason"]
    for column in ("Unit Cost", "Quantity"):
        assert cell(result, "Details", column, 1)["status"] == tc.VERIFIED
    assert status_of(result, "line_arithmetic") == tc.FAIL and result["holds"]
    assert "line_arithmetic" in result["failed"]


def test_a_line_with_a_missing_quantity_holds_its_unit_cost():
    issues = [{"code": "Line Parsing Exception", "line": 1, "rule": "ALG-026", "blocking": True}]
    lines = [{"line": 1, "cells": {**line(1, "2", "50.00")["cells"], "Quantity": {"value": "", "reason": "Not found"}}}]
    result = check(view(lines=lines, issues=issues), "Widget 50.00", "Net 100.00 VAT 5.00 Total 105.00")
    assert cell(result, "Details", "Unit Cost", 1)["status"] == tc.CHECK_FAILED
    assert cell(result, "Details", "Quantity", 1)["status"] == tc.EMPTY


def test_an_sl_document_prefix_is_flagged_not_rewritten():
    rows = ("Widget 2 x 50.00 100.00", "Net 100.00", "VAT 5% 5.00", "Total 105.00")
    result = check(view(number="Sl0543"), *rows)
    document = cell(result, "Header", "Document")
    assert document["status"] == tc.CHECK_FAILED and document["value"] == "Sl0543"
    assert "document_prefix" in document["reason"] and result["holds"]
    assert "document_prefix" in result["failed"]
    assert "Sl0543" not in str(result["failed"]) + str([c["detail"] for c in result["checks"]])
    for number in ("SI0543", "0543", "DEMO-0543"):
        got = check(view(number=number), *rows)
        assert cell(got, "Header", "Document")["status"] == tc.VERIFIED, number
        assert status_of(got, "document_prefix") == tc.PASS


def test_the_prefix_lookalike_rule():
    for number in ("Sl0543", "l0543", "INV-Sl/0543", "sI0543"):
        assert tc.lookalike_prefix(number), number
    for number in ("SI0543", "Inv0543", "INV-0543", "0543", "Invl0543", ""):
        assert not tc.lookalike_prefix(number), number


def test_an_owner_entry_is_the_owner_say_and_is_not_held():
    v = view(net="1050.00", tax="50.00", lines=[line(1, "10", "100.00")])
    v["fields"]["net"] = {"value": "1050.00", "evidence": [{"kind": "owner_entry", "source": "Entered by reviewer"}]}
    result = tc.check_view(v, tc.Sources(config=CONFIG, text=text("Net 1000.00 VAT 50.00 Total 1050.00")),
                           entries={"header": {"net": "1050.00"}},
                           attribution={"header:net": {"actor": "owner", "at": "2031-03-03T00:00:00+00:00"}})
    assert cell(result, "Header", "Total Cost Ex Tax")["status"] == tc.VERIFIED
    assert status_of(result, "lines_to_net") == tc.FAIL, "the failed check is still reported"


def test_the_issue_names_the_held_cells_and_no_values():
    v = view(net="1050.00", tax="50.00", lines=[line(1, "10", "100.00")])
    result = check(v, "Widget 10 x 100.00 1000.00", "Net 1000.00", "VAT 5% 50.00", "Total 1050.00")
    held = [i for i in tc.issues(result) if i["code"] == "Target Check Failed"]
    assert len(held) == 1 and "Header.Total Cost Ex Tax" in held[0]["message"] and held[0]["blocking"]
    assert "1050" not in held[0]["message"]


# Owner decision (Tharak, relayed 2026-11-05): exact, but up to AED 10.00, SAR 10.00 or KWD 1.000 may differ. The
# table is the owner config's arithmetic_tolerance; another currency has none: exact, and flagged.
BOUNDARY = (("AED", "10.00", True), ("AED", "10.01", False), ("SAR", "10.00", True), ("SAR", "10.01", False),
            ("KWD", "1.000", True), ("KWD", "1.001", False), ("USD", "0.01", False))


def priced(currency, net, tax, cost="100.00"):
    v = view(net=net, tax=tax, lines=[line(1, "10", cost)])
    v["fields"]["currency"] = {"value": currency}
    return v


def flag_of(result):
    return next(c for c in result["checks"] if c["check"] == "arithmetic_tolerance")


def test_the_lines_may_miss_the_net_by_the_currency_tolerance_only():
    for currency, diff, ok in BOUNDARY:
        places = 3 if currency == "KWD" else 2
        net = f"{1000 + float(diff):.{places}f}"
        result = check(priced(currency, net, "0", f"{100:.{places}f}"), f"Net {net}")
        assert (status_of(result, "lines_to_net") == tc.PASS) is ok, (currency, diff)
        if not ok:
            assert cell(result, "Header", "Total Cost Ex Tax")["status"] == tc.CHECK_FAILED, (currency, diff)


def test_net_plus_tax_may_miss_the_printed_gross_by_the_currency_tolerance_only():
    for currency, diff, ok in BOUNDARY:
        places = 3 if currency == "KWD" else 2
        gross = f"{1050 + float(diff):.{places}f}"
        result = check(priced(currency, f"{1000:.{places}f}", f"{50:.{places}f}"), f"Total {gross}")
        assert (status_of(result, "net_plus_tax_gross") == tc.PASS) is ok, (currency, diff)


def test_the_net_and_tax_themselves_never_stand_in_for_the_gross():
    # Net 2000.00 is within AED 10 of net + a stray 7.00; it is the net, not a printed gross.
    result = check(priced("AED", "2000.00", "7.00", "200.00"), "Net 2000.00", "7.00")
    assert status_of(result, "net_plus_tax_gross") == tc.WARNING


def test_the_tax_is_exact_plus_rounding_not_the_before_tax_tolerance():
    # The owner's tolerance is for the total before tax; the tax may miss net x rate by one minor unit per line.
    for currency, tax, ok in (("AED", "50.01", True), ("AED", "50.02", False), ("AED", "55.00", False),
                              ("KWD", "50.001", True), ("KWD", "50.002", False)):
        places = 3 if currency == "KWD" else 2
        result = check(priced(currency, f"{1000:.{places}f}", tax, f"{100:.{places}f}"), f"Tax {tax}")
        assert (status_of(result, "tax_breakdown_to_header") == tc.PASS) is ok, (currency, tax)
        if not ok:
            assert cell(result, "Header", "Tax Amount")["status"] == tc.CHECK_FAILED, (currency, tax)


def test_a_header_net_off_by_five_aed_passes():
    result = check(priced("AED", "1005.00", "50.25"), "Net 1005.00", "VAT 50.25", "Total 1055.25")
    assert status_of(result, "lines_to_net") == tc.PASS and status_of(result, "net_plus_tax_gross") == tc.PASS
    assert cell(result, "Header", "Total Cost Ex Tax")["status"] == tc.VERIFIED


def test_without_a_labelled_gross_a_line_amount_near_net_plus_tax_does_not_verify():
    # No line is labelled as the total; a line amount happens to be within AED 10 of net + tax.
    v = priced("AED", "1000.00", "50.00")
    result = check(v, "Widget 10 x 100.00 1000.00", "Fees 1 x 1052.00 1052.00", "Net 1000.00", "VAT 50.00")
    assert status_of(result, "net_plus_tax_gross") == tc.WARNING
    assert cell(result, "Header", "Total Cost Ex Tax")["status"] == tc.CHECK_FAILED
    labelled = check(v, "Widget 10 x 100.00 1000.00", "Net 1000.00", "VAT 50.00", "Grand Total 1052.00")
    assert status_of(labelled, "net_plus_tax_gross") == tc.PASS
    for not_gross in ("Sub Total 1052.00", "Total excl. VAT 1052.00", "Total lines: 1052"):
        assert status_of(check(v, not_gross), "net_plus_tax_gross") == tc.WARNING, not_gross


def test_a_currency_without_a_tolerance_is_exact_and_flagged():
    result = check(priced("USD", "1000.01", "0"))
    assert status_of(result, "lines_to_net") != tc.PASS
    assert flag_of(result)["status"] == tc.WARNING and flag_of(result)["detail"] == "no tolerance set for USD"
    assert flag_of(check(priced("AED", "1000.00", "50.00")))["status"] == tc.PASS


def test_the_owner_config_replaces_the_tolerance_table():
    config = {**CONFIG, "arithmetic_tolerance": {"USD": "0.05"}}
    result = tc.check_view(priced("USD", "1000.05", "0"), tc.Sources(config=config, text=text()))
    assert status_of(result, "lines_to_net") == tc.PASS and flag_of(result)["status"] == tc.PASS
    result = tc.check_view(priced("AED", "1000.01", "0"), tc.Sources(config=config, text=text()))
    assert status_of(result, "lines_to_net") != tc.PASS and flag_of(result)["detail"] == "no tolerance set for AED"


def line_exceptions(currency, net_amount, config=None, qty="1", price=None):
    from app.fine_rules import RowsSource, RulesConfig, run_invoice
    from app.models import Invoice, Line
    source = RowsSource([{"VPN": "VPN-1", "ITEM_PARENT": "P1", "SUPPLIER": "101", "SUPPLIER_NAME": "ABC001RE4AED",
                          "SUPPLIER SITE NAME": "Synthetic Co", "ITEM_DESC": "Rose red"}])
    invoice = Invoice(supplier_name="Synthetic Co", currency=currency, lines=[
        Line(sku="VPN-1", description="Rose red", qty=qty,
             price=price or ("100.000" if currency == "KWD" else "100.00"), net_amount=net_amount)])
    result = run_invoice(invoice, source, config or RulesConfig(), text_value="INVOICE\nRose red")
    return [e for e in result["exceptions"] if e["Rule ID"] == "02A-Unit Cost"], result


def test_a_line_is_exact_plus_rounding_not_the_before_tax_tolerance():
    # Owner answer: the tolerance is for the total before tax; per line one minor unit per unit of quantity.
    for currency, qty, price, amount, held in (("AED", "3", "5.00", "10.00", True),    # qty 2 read as 3: off by 5
                                               ("AED", "1", "100.00", "105.00", True),  # a line off by AED 5
                                               ("AED", "3", "3.33", "10.00", False),    # 9.99: a rounded unit cost
                                               ("AED", "3", "3.33", "10.03", True),
                                               ("KWD", "2", "1.000", "2.002", False), ("KWD", "2", "1.000", "2.003", True)):
        assert bool(line_exceptions(currency, amount, qty=qty, price=price)[0]) is held, (currency, qty, price, amount)


def test_the_line_tolerance_is_its_own_owner_config_key():
    from app.fine_rules import RulesConfig
    config = RulesConfig.from_dict({"line_arithmetic_tolerance": {"AED": "5.00"}})
    assert not line_exceptions("AED", "105.00", config)[0]
    assert line_exceptions("AED", "105.01", config)[0]
    assert line_exceptions("SAR", "100.02", config)[0], "a currency left out stays exact plus rounding"
    for key in ("arithmetic_tolerance", "line_arithmetic_tolerance"):
        for bad in ({"AED": "-1"}, {"aed": "1"}, ["AED"]):
            try:
                RulesConfig.from_dict({key: bad})
            except ValueError:
                continue
            raise AssertionError((key, bad))


def test_an_owner_table_with_only_aed_makes_sar_exact_and_flagged():
    config = {**CONFIG, "arithmetic_tolerance": {"AED": "10.00"}}
    result = tc.check_view(priced("SAR", "1000.01", "0"), tc.Sources(config=config, text=text("Net 1000.01")))
    assert status_of(result, "lines_to_net") != tc.PASS
    assert flag_of(result)["detail"] == "no tolerance set for SAR"


# Owner decision B (Q-a, 2026-11-05): R-016 picks the VAT code when the printed tax is within the before-tax
# tolerance table of net x rate; the target check's tax vs net x rate stays rounding-only.
VAT_CONFIG = {"vat_codes": [{"region": "ARE", "code": "VATA5", "rate": "5", "active_from": "2020-01-01"},
                            {"region": "KWT", "code": "VATK5", "rate": "5", "active_from": "2020-01-01"}]}


def taxed(currency, tax, config=VAT_CONFIG):
    from app.fine_rules import CURRENCY_REGION, RulesConfig, RowsSource, run_invoice
    from app.matching import rules_view
    from app.models import Invoice, Line
    places = 3 if currency == "KWD" else 2
    net = f"{1000:.{places}f}"
    source = RowsSource([{"VPN": "VPN-1", "ITEM_PARENT": "P1", "SUPPLIER": "101", "SUPPLIER_NAME": "ABC001RE4AED",
                          "SUPPLIER SITE NAME": "Synthetic Co", "ITEM_DESC": "Rose red"}])
    invoice = Invoice(supplier_name="Synthetic Co", currency=currency, date="2031-03-03", net=net, tax=tax, lines=[
        Line(sku="VPN-1", description="Rose red", qty="10", price=f"{100:.{places}f}", net_amount=net)])
    printed = f"INVOICE\nRose red 10 {100:.{places}f} {net}\nNet {net}\nVAT {tax}\nTotal {Decimal(net) + Decimal(tax)}"
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(CURRENCY_REGION, "KWD", "KWT")  # a VAT region for the KWD boundary (synthetic)
        result = run_invoice(invoice, source, RulesConfig.from_dict(config), text_value=printed)
        view = rules_view(result, printed)
        return result, tc.check_view(view, tc.Sources(config=config, text=printed))


def test_r016_picks_the_vat_code_within_the_tolerance_but_the_tax_check_still_holds():
    for currency, tax, filled in (("AED", "50.00", True), ("AED", "60.00", True), ("AED", "60.01", False),
                                  ("AED", "40.00", True), ("KWD", "51.000", True), ("KWD", "51.001", False)):
        result, checked = taxed(currency, tax)
        code = result["lines"][0]["Unit Tax Code"]
        assert bool(code) is filled, (currency, tax)
        exact = tax in ("50.00",)
        assert (status_of(checked, "tax_breakdown_to_header") == tc.PASS) is exact, (currency, tax)
        if filled and not exact:
            assert cell(checked, "Header", "Tax Amount")["status"] != tc.VERIFIED, (currency, tax)


def test_r016_is_exact_for_a_currency_without_a_tolerance():
    result, _ = taxed("AED", "50.01", {**VAT_CONFIG, "arithmetic_tolerance": {"SAR": "10.00"}})
    assert result["lines"][0]["Unit Tax Code"] == ""
    result, _ = taxed("AED", "50.00", {**VAT_CONFIG, "arithmetic_tolerance": {"SAR": "10.00"}})
    assert result["lines"][0]["Unit Tax Code"] == "VATA5"
