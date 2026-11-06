"""The item master rules on the local read, before the AI is asked (docs/AI_BOUNDARY.md).

The rule chain is the rules workbook's (item_rules.rules()): the item step through matching.lookup_item, which
runs the workbook's lookup_order (VPN -> barcode -> description), then tax by country. The item master search
for one line is ``item_search(line) -> search(column, match)``, the caller's; nothing here searches.
A rule fills a field only when it is empty and only on a Found result; a suggestion, conflict or ambiguous
result fills nothing and is held for the reviewer. Every filled field names its rule ("rule:<id>").
"""
from dataclasses import dataclass, field

from . import matching

TAX_RULE = "T5.1"


@dataclass
class Resolved:
    invoice: dict
    sources: dict = field(default_factory=lambda: {"header": {}, "lines": []})
    held: list = field(default_factory=list)   # lookup_item results that need the reviewer, with their line


def _blank(value):
    return value is None or str(value).strip() == ""


def resolve(invoice, item_search, ruleset):
    """Run the rule chain on a local read (a dict); returns a copy with only empty fields filled."""
    invoice = {**invoice, "lines": [dict(line) for line in invoice.get("lines") or []]}
    out = Resolved(invoice)
    for n, line in enumerate(invoice["lines"], 1):
        filled = {}
        if item_search:
            found = matching.lookup_item(item_search(line), ruleset)
            if found["status"] == "Found":
                if _blank(line.get("item_id")):
                    line["item_id"] = str(found["parents"][0])
                    filled["item_id"] = f"rule:{found['rule']}"
            else:
                out.held.append({"line": n, **found})
        out.sources["lines"].append(filled)
    if _blank(invoice.get("taxCode")):
        code = next((c for c in (ruleset.tax_code(invoice[k]) for k in ("market", "origin") if invoice.get(k)) if c),
                    None)
        if code:
            invoice["taxCode"] = code
            out.sources["header"]["taxCode"] = f"rule:{TAX_RULE}"
    return out


def rerun_lines(resolved, lines, item_search, ruleset):
    """AI lines stood in for the local ones: the rules run again on them, so a rule value sits on the row it was found for."""
    again = resolve({"lines": lines}, item_search, ruleset)
    resolved.invoice["lines"], resolved.sources["lines"], resolved.held = again.invoice["lines"], again.sources["lines"], again.held


def identified(resolved, n):
    """Line n (1-based) has its item from the item master, so a missing printed code is not a gap."""
    lines = resolved.sources["lines"] if resolved else []
    return n <= len(lines) and "item_id" in lines[n - 1]


def protect(invoice, resolved):
    """Put back every rule-resolved value an AI merge changed: the AI never overwrites the item master.

    Lines are matched by position, so only when the merge kept the local read's line count."""
    for name in resolved.sources["header"]:
        invoice[name] = resolved.invoice[name]
    lines = invoice.get("lines") or []
    if len(lines) == len(resolved.invoice["lines"]):
        for line, mine, filled in zip(lines, resolved.invoice["lines"], resolved.sources["lines"]):
            for name in filled:
                line[name] = mine[name]
    return invoice
