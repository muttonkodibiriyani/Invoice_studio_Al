"""O3.1 / O3.3 with D2 / D3: the Order No comes from the found items' RMS_ORDER_NO, never from the printed PO,
and is never left blank while a found item carries one."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.fine_rules import STORE, WAREHOUSE, RulesConfig
from app.master_orders import choose_order, destination
from app.models import Invoice

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


def test_invoice_quantity_breaks_an_item_tie_across_several_orders():
    """D2: the found items sit on several orders; the order holding the most of them wins, then the most invoice
    quantity of the items it holds."""
    invoiced = {"P1": Decimal(2), "P2": Decimal(2), "P3": Decimal(9), "P4": Decimal(1)}
    pick = choose_order({"A": rows("P1", "P2"), "B": rows("P3", "P4"), "C": rows("P3")}, set(invoiced), invoiced, TIERS)
    assert (pick["order"], pick["covered"], pick["tied"], pick["decided"]) == ("B", 2, [], "quantity")


@pytest.mark.parametrize("invoiced", [{"P1": 1, "P2": 1}, {"P1": None, "P2": None}])
def test_a_tie_quantity_cannot_break_goes_to_the_lowest_order_and_lists_the_tie(invoiced):
    """D3 (provisional): never blank; the lowest tied RMS_ORDER_NO, every tied order listed for review."""
    pick = choose_order({"1000020": rows("P1"), "999999": rows("P2"), "1000010": rows("P1")}, set(invoiced),
                        invoiced, TIERS)
    assert pick["order"] == "999999" and pick["tied"] == ["999999", "1000010", "1000020"]
    assert pick["decided"] == "lowest-tie"


def test_one_order_is_unique():
    assert choose_order({"A": rows("P1")}, {"P1"}, {"P1": 1}, TIERS)["decided"] == "unique"


def test_repeated_order_rows_count_an_item_once():
    pick = choose_order({"A": rows("P1", "P1", "P1"), "B": rows("P1", "P2")}, {"P1", "P2"}, {"P1": 1, "P2": 1}, TIERS)
    assert (pick["order"], pick["covered"]) == ("B", 2)


@pytest.mark.parametrize("groups,parents", [({}, {"P1"}), ({"A": rows("P9")}, {"P1"}), ({"A": rows("P1")}, set())])
def test_no_order_holding_a_matched_item_finds_nothing(groups, parents):
    pick = choose_order(groups, parents, dict.fromkeys(parents, 1), TIERS)
    assert pick["order"] is None and pick["tied"] == []


def place(**data):
    """destination() for one master directory row printed in the Deliver To block."""
    configured = data.pop("configured", None)
    config = RulesConfig(location_master={"12": {"type": configured}} if configured else {})
    row = {"source_hash": "a" * 64, "source_sheet": "Master", "source_row": 2, "data": {"LOCATION NUMBER": "12", **data}}
    return destination(Invoice(location="12"), {"pages": {1: "Deliver To: 12"}}, [row], config)


@pytest.mark.parametrize("data,kind", [({"LOCATION NAME": "STORE"}, STORE), ({"LOCATION NAME": "store(s)"}, STORE),
                                       ({}, STORE), ({"LOCATION NAME": "", "LOCATION NAME 2": "North warehouse"}, WAREHOUSE),
                                       ({"LOCATION NAME": "Warehouse (W)"}, WAREHOUSE),
                                       ({"LOCATION NAME": "Depot"}, STORE)])
def test_location_type_comes_from_the_location_name(data, kind):
    """D6: a name containing 'warehouse' is a warehouse, any other name a store."""
    assert place(**data)["kind"] == kind


def test_a_configured_type_that_contradicts_the_row_is_held():
    assert place(**{"LOCATION NAME": "Store", "configured": WAREHOUSE}) is None
