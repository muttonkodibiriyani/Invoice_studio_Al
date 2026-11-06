"""D8: a printed supplier name one typo or a trailing '.' away from the master name still finds the supplier, but
only when exactly one master name is that close; otherwise the supplier stays not found with the near names listed.
Synthetic data only."""
import pytest

from app.fine_rules import RowsSource, RulesConfig, run_invoice, supplier_candidates
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


def keyword(result):
    return [t for t in tolerant(result) if "key words" in t["source"]]


@pytest.mark.parametrize("printed", ["ALMALUXE", "Almaluxe Trading", "ALMALUXE L.L.C.", "Almaluxx General Trading"])
def test_r1_the_key_word_finds_the_one_master_that_has_it(printed):
    result = run(printed, master("Almaluxe General Trading LLC", "Almadar Flowers Co", "Bushbalm Est"))
    assert result["header"]["Supplier Site"] == "101" and not not_found(result)
    [trace] = keyword(result)
    assert (trace["original"], trace["value"], trace["rule"]) == (printed, "Almaluxe General Trading LLC", "SUP-001")


@pytest.mark.parametrize("printed", ["ALM", "Alm Trading LLC", "Almadar", "Bushbalm", "Balm Trading", "Almaluxe Flowers",
                                     "Zafran Trading"])
def test_r1_a_key_word_is_whole_never_part_of_another(printed):
    result = run(printed, master("Almaluxe General Trading LLC", "Palm Trading Co", "Kalma Est"))
    assert result["header"]["Supplier Site"] == "" and not_found(result) and not tolerant(result)


def test_r1_two_masters_with_the_key_word_stay_in_review_with_both_listed():
    result = run("Almaluxe", master("Almaluxe General Trading LLC", "Almaluxe Flowers FZE"))
    assert result["header"]["Supplier Site"] == "" and not_found(result) and not tolerant(result)
    [review] = [e for e in result["exceptions"] if e["Rule ID"] == "SUP-001" and "key words" in e["Description"]]
    assert review["Candidates / Evidence"] == "Almaluxe Flowers FZE; Almaluxe General Trading LLC"


@pytest.mark.parametrize("printed, site", [("Synthetik Trading", "101"), ("Abd Trading", "")])
def test_r1_one_typo_only_on_a_key_word_of_six_or_more(printed, site):
    result = run(printed, master("Synthetic General Trading LLC", "Abc Flowers Co"))
    assert result["header"]["Supplier Site"] == site
    assert bool(keyword(result)) == bool(site)


def test_r1_a_name_unlike_the_master_is_not_guessed():
    # An issuer name that shares no key word with its master needs an owner alias, never a guess.
    result = run("Northwind Floral Supplies", master("Almaluxe General Trading LLC", "Synthetic Flowers Co"))
    assert result["header"]["Supplier Site"] == "" and not_found(result) and not tolerant(result)


@pytest.mark.parametrize("printed", ["General Trading LLC", "GENERAL TRADING L.L.C.", "The Trading Group"])
def test_r1_a_name_of_generic_words_only_finds_no_supplier(printed):
    # Nothing is left once the generic words go: an empty key-word set matches no master, it never matches all.
    result = run(printed, master("Almaluxe General Trading LLC", "Synthetic Flowers Co"))
    assert result["header"]["Supplier Site"] == "" and not_found(result) and not keyword(result)


LUX_FAMILY = ("Almaluxe General Trading LLC", "Luxor Flowers Co", "Deluxe Blooms Est")


@pytest.mark.parametrize("printed", ["LUX", "Luxe Trading", "Lux General Trading LLC"])
def test_r1_a_short_shared_stem_crosses_into_no_lux_family_name(printed):
    result = run(printed, master(*LUX_FAMILY))
    assert result["header"]["Supplier Site"] == "" and not_found(result) and not keyword(result)


@pytest.mark.parametrize("printed, site", [("Almaluxe", "101"), ("LUXOR", "102"), ("Deluxe Blooms", "103")])
def test_r1_each_lux_family_name_finds_only_its_own(printed, site):
    result = run(printed, master(*LUX_FAMILY))
    assert result["header"]["Supplier Site"] == site and not not_found(result)


OWNER = {"buyer_name": "Orchid Retail LLC"}


@pytest.mark.parametrize("printed", ["Orchid Retail", "ORCHID RETAIL LLC", "Orchid Retail L.L.C."])
def test_the_configured_buyer_is_never_the_supplier(printed):
    # The reader has no config, so a footer name or an unlabelled buyer it took for the issuer stops here.
    result = run(printed, master(printed, "Other Co"), RulesConfig.from_dict(OWNER))
    assert result["header"]["Supplier Site"] == "" and not result["supplier_candidates"]


@pytest.mark.parametrize("printed", ["Orchid Retail FZE", "Orchid Retail Trading LLC"])
def test_a_group_company_of_the_buyer_stays_a_supplier(printed):
    result = run(printed, master(printed, "Other Co"), RulesConfig.from_dict(OWNER))
    assert result["header"]["Supplier Site"] == "101"


def test_with_no_configured_buyer_the_printed_name_stands():
    assert run("Orchid Retail", master("Orchid Retail", "Other Co"))["header"]["Supplier Site"] == "101"


def test_the_configured_buyer_on_a_line_of_the_page_is_no_candidate():
    candidates = supplier_candidates({"pages": {1: "Orchid Retail LLC\nHarbour Lane LLC"}}, owner_name="Orchid Retail LLC")
    assert [candidate["name"] for candidate in candidates] == ["Harbour Lane LLC"]
