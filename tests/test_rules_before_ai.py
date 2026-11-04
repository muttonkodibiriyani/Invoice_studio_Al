"""The item master rules run on the local read before the AI is asked: the AI is called only for what the
local readers and the rules both left open, and it never overwrites a value the rules resolved."""
import json
import pathlib
import types

import pytest

from app import engines, item_rules, rules_first
from app.models import Invoice, ProcessingOptions

ROOT = pathlib.Path(__file__).resolve().parents[1]
INVOICE = json.loads((ROOT / "samples" / "invoice.json").read_text())
TEXT = "Lumena Beauty Trading LLC\nInvoice DEMO-2026-001\n" + "ITEMS BEGIN Lumena serum 30 ml 10 60.00\n" * 8
# The local read printed no supplier code or barcode on either line: an item identity gap per line.
NO_IDS = {**INVOICE, "market": "UAE", "lines": [{**line, "sku": None, "gtin": None} for line in INVOICE["lines"]]}
PARENT = {"Lumena serum 30 ml": "1001", "Lumena cream 50 ml": "1002"}


def rule(rule_id, step=2, order=1, columns=("VPN",), match="exact", confirm=False):
    return item_rules.LookupRule(rule_id, step, f"{step}", order, "", columns, match, confirm, None, "", "", "", ())


class FakeRules:
    def __init__(self, lookup, taxes=None):
        self.lookup, self.taxes = lookup, taxes or {}

    def lookup_order(self, step=None):
        return tuple(r for r in self.lookup if step is None or r.step == step)

    def tax_code(self, country):
        return self.taxes.get(str(country or "").casefold())


def matcher(hits, asked=None):
    """The item master search stand-in: a line's ITEM_PARENT for the columns in ``hits``; records each column asked."""
    def item_search(line):
        def search(column, match):
            if asked is not None:
                asked.append(column)
            return [PARENT[line["description"]]] if column in hits else []
        return search
    return item_search


@pytest.fixture
def run(monkeypatch, tmp_path):
    (tmp_path / "work").mkdir()
    monkeypatch.delenv("INV_STUDIO_AI_CROSS_CHECK", raising=False)
    monkeypatch.setattr(engines, "capabilities", lambda: [
        {"id": e, "installed": e == "invoice2data", "models_blocked": False, "version": "1", "note": ""}
        for e in ("invoice2data", "paddleocr", "docling")])

    def go(read, match, rules, ai=INVOICE, **options):
        calls = []

        def loaded():
            if isinstance(rules, Exception):
                raise rules
            return rules
        monkeypatch.setattr(engines.item_rules, "rules", loaded)
        monkeypatch.setattr(engines, "local_read", lambda engine, path, root, language="en", extra=(): {
            "text": TEXT, "invoice": read, "boxes": [], "tables": []})

        def foundry(path, text, opts):  # the AI client: every call is counted
            calls.append("ai")
            return Invoice.model_validate(ai), {"evidence": {}}

        result = engines.process(ROOT / "samples" / "invoice.pdf",
                                 ProcessingOptions(**{"engine": "invoice2data", "model": "m", **options}),
                                 types.SimpleNamespace(root=tmp_path), foundry, item_search=match)
        return result, calls
    return go


RULES = FakeRules((rule("I2.1"), rule("I2.2", order=2, columns=("BARCODE", "BARCODE_1")),
                   rule("I2.3", order=3, columns=("ITEM_DESC",), match="fuzzy", confirm=True),
                   rule("I2.5", order=5, columns=())), {"uae": "VAT5"})


def test_when_local_and_rules_resolve_every_field_the_ai_is_not_called(run):
    result, calls = run(NO_IDS, matcher({"VPN"}), RULES)
    assert calls == []
    ai = result["readers"]["ai"]
    assert ai["calls"] == 0 and ai["need"] == "cross_check" and ai["fields"] == []
    assert [x["item_id"] for x in result["invoice"]["lines"]] == ["1001", "1002"]
    assert result["invoice"]["taxCode"] == "VAT5"


def test_the_ai_is_asked_only_for_the_fields_the_rules_left_open_and_never_overwrites_them(run):
    read = {**NO_IDS, "date": None}
    # The AI reads other ids and another tax code: none of them may replace what the rules resolved.
    ai = {**INVOICE, "taxCode": "ZERO", "lines": [{**line, "item_id": "9999"} for line in INVOICE["lines"]]}
    result, calls = run(read, matcher({"VPN"}), RULES, ai=ai)
    assert calls == ["ai"]
    assert result["readers"]["ai"]["fields"] == ["date"]
    assert result["invoice"]["date"] == INVOICE["date"] and result["readers"]["header"]["date"] == "ai"
    assert [x["item_id"] for x in result["invoice"]["lines"]] == ["1001", "1002"]
    assert result["invoice"]["taxCode"] == "VAT5"


def test_an_ai_read_that_replaces_the_whole_local_read_still_keeps_the_rule_resolved_values(run):
    # The AI chosen as the reader takes over a weaker local read; the item master's values stay.
    result, calls = run({**NO_IDS, "net": None}, matcher({"VPN"}), RULES, engine="ai", provider="claude_local")
    assert calls == ["ai"] and result["selected_engine"] == "claude_local / m"
    assert [x["item_id"] for x in result["invoice"]["lines"]] == ["1001", "1002"]
    assert result["invoice"]["taxCode"] == "VAT5"
    assert [x["item_id"] for x in result["readers"]["lines"]] == ["rule:I2.1", "rule:I2.1"]


def test_the_chain_runs_in_the_rule_sets_lookup_order_not_a_hardcoded_one():
    asked = []
    order = FakeRules((rule("B", order=1, columns=("BARCODE",)), rule("V", order=2), rule("N", order=3, columns=())))
    resolved = rules_first.resolve({"lines": [{"description": "Lumena serum 30 ml"}]}, matcher({"BARCODE"}, asked), order)
    assert asked == ["BARCODE", "VPN"]
    assert resolved.sources["lines"] == [{"item_id": "rule:B"}]


def test_the_shipped_workbook_order_is_vpn_then_barcode_then_description():
    asked = []
    rules_first.resolve({"lines": [{"description": "Lumena serum 30 ml"}]}, matcher(set(), asked), item_rules.rules())
    assert asked[:4] == ["VPN", "BARCODE", "BARCODE_1", "ITEM_DESC"]


def test_a_suggestion_fills_nothing_and_is_held_for_the_reviewer():
    resolved = rules_first.resolve({"lines": [{"description": "Lumena serum 30 ml"}]}, matcher({"ITEM_DESC"}), RULES)
    assert resolved.invoice["lines"][0].get("item_id") is None and resolved.sources["lines"] == [{}]
    assert [(h["line"], h["rule"], h["status"]) for h in resolved.held] == [(1, "I2.3", "Suggestion")]


def test_a_read_item_is_never_replaced_by_the_item_master():
    resolved = rules_first.resolve({"lines": [{"description": "Lumena serum 30 ml", "item_id": "READ"}]},
                                   matcher({"VPN"}), RULES)
    assert resolved.invoice["lines"][0]["item_id"] == "READ" and resolved.sources["lines"] == [{}]


def test_each_value_names_its_source(run):
    result, _ = run({**NO_IDS, "date": None}, matcher({"VPN"}), RULES)
    readers = result["readers"]
    assert readers["header"]["number"] == "invoice2data"
    assert readers["header"]["taxCode"] == "rule:T5.1"
    assert readers["header"]["date"] == "ai"
    assert readers["lines"][0]["item_id"] == "rule:I2.1" and readers["lines"][0]["qty"] == "invoice2data"
    trace = {t["engine"]: t for t in result["trace"]}
    assert trace["item_rules"]["status"] == "resolved"


def test_with_ai_off_the_read_completes_on_local_and_rules_and_flags_what_is_open(run):
    result, calls = run({**NO_IDS, "date": None}, matcher({"VPN"}), RULES, ai_fallback=False)
    assert calls == []
    assert result["readers"]["ai"]["status"] == "off" and result["readers"]["ai"]["fields"] == ["date"]
    assert "date" in result["readers"]["gaps"]
    assert not any("item identity" in gap for gap in result["readers"]["gaps"])
    assert result["invoice"]["lines"][0]["item_id"] == "1001"


def test_a_rule_set_that_cannot_load_is_told_and_the_read_goes_on(run):
    refused = item_rules.RulesError("Rules workbook x.xlsx does not match its checksum")
    result, calls = run(NO_IDS, matcher({"VPN"}), refused, ai_fallback=False)
    trace = {t["engine"]: t for t in result["trace"]}
    assert trace["item_rules"]["status"] == "unavailable" and "checksum" in trace["item_rules"]["reason"]
    assert any("item identity" in gap for gap in result["readers"]["gaps"])
