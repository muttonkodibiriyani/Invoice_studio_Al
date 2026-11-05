"""D8: a printed supplier name one typo or a trailing '.' away from the master name still finds the supplier, but
only when exactly one master name is that close; otherwise the supplier stays not found with the near names listed.
Synthetic data only."""
import pytest

from app.fine_rules import RowsSource, RulesConfig, run_invoice
from app.fine_rules_source import LookupRulesSource
from app.models import Invoice, Line
from tests.test_latest_master import activate


def master(*names):
    # One item per SUPPLIER SITE NAME, on sites 101, 102, ...
    return RowsSource([{"VPN": f"VPN-{n}", "ITEM_PARENT": f"P{n}", "SUPPLIER": str(100 + n),
                        "SUPPLIER_NAME": f"ABC00{n}RE4AED", "SUPPLIER SITE NAME": name, "ITEM_DESC": "Rose red"}
                       for n, name in enumerate(names, 1)])


def run(printed, source, config=None):
    invoice = Invoice(supplier_name=printed, lines=[Line(sku="VPN-1", description="Rose red")])
    return run_invoice(invoice, source, config or RulesConfig(), text_value="INVOICE\nRose red")


def not_found(result):
    return [e for e in result["exceptions"] if e["Description"].startswith("Supplier not found")]


def tolerant(result):
    return [t for t in result["lineage"] if t["target"] == "Supplier Name" and t["confidence"] == "Tolerant"]


@pytest.mark.parametrize("printed", ["Synthetik Trading Co.", "SYNTHETIC TRADING  CO .", "Synthetic Tradng Co"])
def test_one_close_master_name_finds_the_supplier(printed):
    result = run(printed, master("Synthetic Trading Co", "Other Co"))
    assert result["header"]["Supplier Site"] == "101"
    assert not not_found(result)
    if printed != "SYNTHETIC TRADING  CO .":  # that one is exact once case and punctuation go
        [trace] = tolerant(result)
        assert (trace["original"], trace["value"], trace["rule"]) == (printed, "Synthetic Trading Co", "SUP-001")


def test_two_close_master_names_stay_not_found_with_both_listed():
    result = run("Synthetic Trading C.", master("Synthetic Trading Co", "Synthetic Trading Ca"))
    assert result["header"]["Supplier Site"] == ""
    assert not_found(result) and not tolerant(result)
    [review] = [e for e in result["exceptions"] if e["Rule ID"] == "SUP-001" and "close to" in e["Description"]]
    assert review["Candidates / Evidence"] == "Synthetic Trading Ca; Synthetic Trading Co"


@pytest.mark.parametrize("printed", ["Abd Co", "Synthetic Trdng Coo"])
def test_a_short_name_or_a_far_name_is_not_guessed(printed):
    result = run(printed, master("Abc Co", "Synthetic Trading Co"))
    assert result["header"]["Supplier Site"] == "" and not_found(result) and not tolerant(result)


def test_a_close_supplier_sites_name_bridges_to_its_code():
    config = RulesConfig.from_dict({"supplier_sites": [
        {"supplier_site": "101", "currency": "AED", "status": "Active", "supplier_name": "Synthetic Holdings LLC",
         "supplier_code": "ABC001", "site_name": "ABC001RE4AED"}]})
    result = run("Synthetik Holdings L.L.C.", master("Coded Site Name"), config)
    assert result["header"]["Supplier Site"] == "101" and not not_found(result)
    assert tolerant(result)[0]["value"] == "Synthetic Holdings LLC"


def test_the_catalog_lists_its_supplier_names(client, owner, app):
    activate(client, owner)
    names = LookupRulesSource(app.state.store).supplier_names()
    assert {"synthetic co", "other co"} <= set(names)
    assert LookupRulesSource(app.state.store).items_by_supplier_name(next(n for n in names if n == "synthetic co"))
