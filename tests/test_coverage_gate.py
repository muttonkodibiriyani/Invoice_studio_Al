"""V7.1 match coverage: the share of lines the rules found decides the Coverage_Tiers tier and whether export may run."""
from decimal import Decimal
from typing import NamedTuple

import pytest

from app.coverage import coverage, refusal


class Tier(NamedTuple):
    name: str
    low: Decimal
    high: Decimal | None
    export: str
    prompt: str | None


# The rules workbook's Coverage_Tiers rows (v0.2), as RuleSet.coverage_tiers() hands them over.
TIERS = (Tier("Target", Decimal(100), None, "yes", None),
         Tier("First fallback", Decimal(95), Decimal(100), "after_fill", "P-COV"),
         Tier("Second fallback", Decimal(90), Decimal(95), "after_fill", "P-COV"),
         Tier("Below 90%", Decimal(0), Decimal(90), "no", "P-COV-LOW"))


def found():
    return {"value": "1001", "evidence": [{"kind": "lookup", "rule": "I2.1"}]}


def typed():
    return {"value": "2002", "evidence": [{"kind": "owner_entry", "rule": ""}]}


def empty():
    return {"value": None, "evidence": []}


def view(*items):
    return {"lines": [{"line": n, "cells": {"Item": item}} for n, item in enumerate(items, 1)]}


def test_every_line_found_is_the_target_tier_and_exports():
    result = coverage(view(found(), found()), TIERS)
    assert result == {"matched": 2, "total": 2, "pct": "100.00", "tier": "Target", "export": "yes", "low": False, "prompt": None,
                      "missing": []}
    assert refusal(result) is None


@pytest.mark.parametrize("matched,total,tier", [(19, 20, "First fallback"), (18, 20, "Second fallback"),
                                                (17, 20, "Below 90%"), (99, 100, "First fallback"),
                                                (94, 100, "Second fallback"), (89, 100, "Below 90%")])
def test_the_tier_is_inclusive_at_its_lower_bound(matched, total, tier):
    result = coverage(view(*[found()] * matched, *[empty()] * (total - matched)), TIERS)
    assert (result["matched"], result["total"], result["tier"]) == (matched, total, tier)


def test_pct_never_rounds_up_into_the_next_tier():
    result = coverage(view(*[found()] * 1999, empty()), TIERS)
    assert (result["pct"], result["tier"]) == ("99.95", "First fallback")
    result = coverage(view(*[found()] * 899999, empty(), *[empty()] * 99999), TIERS)
    assert result["tier"] == "Below 90%" and result["pct"] == "89.99"


def test_an_invoice_without_lines_has_nothing_to_export():
    result = coverage(view(), TIERS)
    assert (result["matched"], result["total"], result["pct"], result["tier"], result["export"]) == (0, 0, "0.00", "Below 90%", "no")
    assert "no lines" in refusal(result)


def test_a_reviewer_entry_fills_a_gap_but_is_not_a_match():
    result = coverage(view(*[found()] * 18, typed(), empty()), TIERS)
    assert (result["matched"], result["tier"], result["export"], result["prompt"]) == (18, "Second fallback", "after_fill", "P-COV")
    assert result["missing"] == [{"line": 19, "filled": True}, {"line": 20, "filled": False}]


def test_a_fallback_tier_exports_only_once_every_gap_is_filled():
    open_gap = coverage(view(*[found()] * 19, empty()), TIERS)
    assert refusal(open_gap) == "Coverage 19 of 20 lines (95.00%). Fill or confirm line 20 before export."
    assert refusal(coverage(view(*[found()] * 19, typed()), TIERS)) is None


def test_second_fallback_is_flagged_as_low_coverage():
    low = coverage(view(*[found()] * 9, empty()), TIERS)
    assert low["low"] and not coverage(view(*[found()] * 19, empty()), TIERS)["low"]
    assert refusal(low).startswith("Low coverage: 9 of 10 lines (90.00%)")


def test_below_the_last_fallback_export_is_refused_even_when_every_gap_is_typed():
    result = coverage(view(*[found()] * 8, typed(), typed()), TIERS)
    assert (result["tier"], result["export"], result["prompt"]) == ("Below 90%", "no", "P-COV-LOW")
    assert refusal(result) == ("Coverage 8 of 10 lines (80.00%). Add the missing items (lines 9, 10) to the item master, "
                               "then Update item master.")


def test_a_lines_item_with_no_value_or_no_evidence_is_missing():
    no_evidence = {"value": "1001", "evidence": []}
    result = coverage(view(found(), no_evidence, {}), TIERS)
    assert result["matched"] == 1 and [m["line"] for m in result["missing"]] == [2, 3]


def test_tiers_must_cover_every_coverage():
    with pytest.raises(ValueError, match="Coverage_Tiers"):
        coverage(view(found(), empty()), TIERS[:1])
