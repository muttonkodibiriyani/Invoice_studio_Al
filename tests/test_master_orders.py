"""O3.1 / O3.3: the Order No comes from the matched items' RMS_ORDER_NO, never from the printed PO."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.master_orders import choose_order

TIERS = (SimpleNamespace(name="Target", low=Decimal(100), high=None, export_mode="yes"),
         SimpleNamespace(name="First fallback", low=Decimal(95), high=Decimal(100), export_mode="after_fill"),
         SimpleNamespace(name="Second fallback", low=Decimal(90), high=Decimal(95), export_mode="after_fill"),
         SimpleNamespace(name="Below 90%", low=Decimal(0), high=Decimal(90), export_mode="no"))


def rows(*parents):
    return [{"RMS_ITEM_ID": p} for p in parents]


def items(n):
    return [f"P{i}" for i in range(1, n + 1)]


def test_one_order_holding_every_matched_item_is_found():
    pick = choose_order({"450001": rows("P1", "P2")}, {"P1", "P2"}, {"P1": 1, "P2": 1}, TIERS)
    assert (pick["order"], pick["covered"], pick["total"], pick["tier"].name) == ("450001", 2, 2, "Target")


def test_the_order_covering_the_most_matched_items_wins_and_its_share_picks_the_tier():
    parents = set(items(10))
    pick = choose_order({"A": rows(*items(9)), "B": rows(*items(5))}, parents, dict.fromkeys(parents, 1), TIERS)
    assert (pick["order"], pick["covered"], pick["tier"].name) == ("A", 9, "Second fallback")


@pytest.mark.parametrize("covered,tier", [(20, "Target"), (19, "First fallback"), (18, "Second fallback"),
                                          (17, "Below 90%")])
def test_tier_boundaries_are_exact(covered, tier):
    parents = set(items(20))
    pick = choose_order({"A": rows(*items(covered))}, parents, dict.fromkeys(parents, 1), TIERS)
    assert pick["tier"].name == tier


def test_invoice_quantity_breaks_a_share_tie():
    invoiced = {"P1": Decimal(5), "P2": Decimal(1)}
    pick = choose_order({"A": rows("P2"), "B": rows("P1")}, set(invoiced), invoiced, TIERS)
    assert pick["order"] == "B" and pick["tied"] == []


@pytest.mark.parametrize("invoiced", [{"P1": 1, "P2": 1}, {"P1": None, "P2": None}])
def test_a_tie_quantity_cannot_break_is_left_to_the_user(invoiced):
    pick = choose_order({"A": rows("P1"), "B": rows("P2")}, set(invoiced), invoiced, TIERS)
    assert pick["order"] is None and pick["tied"] == ["A", "B"]


def test_repeated_order_rows_count_an_item_once():
    pick = choose_order({"A": rows("P1", "P1", "P1"), "B": rows("P1", "P2")}, {"P1", "P2"}, {"P1": 1, "P2": 1}, TIERS)
    assert (pick["order"], pick["covered"]) == ("B", 2)


@pytest.mark.parametrize("groups,parents", [({}, {"P1"}), ({"A": rows("P9")}, {"P1"}), ({"A": rows("P1")}, set())])
def test_no_order_holding_a_matched_item_finds_nothing(groups, parents):
    pick = choose_order(groups, parents, dict.fromkeys(parents, 1), TIERS)
    assert pick["order"] is None and pick["tied"] == []
