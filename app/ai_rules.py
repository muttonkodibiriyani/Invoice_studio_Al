"""Rules the AI added: supplier + printed code or description on a line -> an item master item (docs/AI_BOUNDARY.md).

There is no approval step (owner, 6 Nov): a rule is added when the AI, given item master candidates this server searched
for a line, names one as that line's item. It runs at once and on every later invoice from that supplier. It fills only
an item cell the rules left empty (engines.process checks the rules' own result first), its value is the item master's,
checked again each time it is used, and every use is marked as AI. Each rule keeps where it came from and can be
removed. Takes no model output but the candidate id app.ai_context has already checked.
"""
import re
import uuid
from datetime import datetime, timezone

from .ai_context import close_names
from .item_search import invoice_sites

STORE_KEY = "ai_added_rules"
MAX_RULES = 2000
MAX_PER_READ = 10
MIN_CODE = 3  # characters of a printed code a rule may key on
MIN_DESCRIPTION = 8


def _fold(value):
    return re.sub(r"[\W_]", "", str(value or "").casefold())  # letters and digits of any script


def _keys(line):
    """What a line prints, strongest first: (kind, folded value) for a code, then the description."""
    for kind, value in (("sku", line.get("sku")), ("gtin", line.get("gtin")),
                        ("gtin", line.get("barcode_unchecked")), ("description", line.get("description"))):
        folded = _fold(value)
        if len(folded) >= (MIN_DESCRIPTION if kind == "description" else MIN_CODE):
            yield kind, folded


def all_rules(store, connection=None):
    return store.get(STORE_KEY, [], connection) if connection is not None else store.get(STORE_KEY, [])


def _logged(rule):
    """What the audit log keeps of a rule, so a removed one still leaves its supplier, item and source."""
    return {k: rule[k] for k in ("id", "supplier_name", "kind", "item_id", "source")}


def add(store, local, suggestions, model="", filename="", sites=()):
    """Add a rule for each item candidate the AI picked for a local line with no item. Returns the new rules' ids.

    sites are the supplier's sites in the item master: a row at another site is not this supplier's, and stays a
    suggestion. A printed name the master does not carry resolves to no site; then only a row whose supplier name is close
    to the printed one makes a rule. Item master SUPPLIER_NAME holds coded site names, so most such picks stay suggestions."""
    supplier = _fold((local or {}).get("supplier_name"))
    lines = (local or {}).get("lines") or []
    picks = [s for s in suggestions if s.get("kind") == "item" and s.get("field") == "item_id"
             and isinstance(s.get("line"), int) and 0 <= s["line"] < len(lines)
             and (s.get("row_site") in sites if sites else close_names((local or {}).get("supplier_name"), s.get("row_supplier")))]
    if not supplier or not picks:
        return []
    ids = []
    with store.connection(True) as c:
        rules = all_rules(store, c)
        held = {(r["supplier"], r["kind"], r["printed"]) for r in rules}
        for pick in picks[:MAX_PER_READ]:
            line = lines[pick["line"]]
            key = next(_keys(line), None)
            if str(line.get("item_id") or "").strip() or key is None or (supplier, *key) in held or len(rules) >= MAX_RULES:
                continue
            held.add((supplier, *key))
            rule = {"id": "air-" + uuid.uuid4().hex[:12], "supplier": supplier, "supplier_name": str(local["supplier_name"])[:200],
                    "kind": key[0], "printed": key[1], "item_id": str(pick["value"]), "source": pick["source"],
                    "why": pick["why"][:300], "model": str(model)[:100], "from_file": str(filename or "")[:200],
                    "added": datetime.now(timezone.utc).isoformat(), "by": "ai", "lines_filled": 0}
            rules = [*rules, rule]
            ids.append(rule["id"])
        if ids:
            store.set(STORE_KEY, rules, c)
            store.audit("ai_rule_added", {"rules": [_logged(r) for r in rules if r["id"] in ids]}, c)
    return ids


def fill(store, source, invoice, site=None):
    """[{line, rule, item_id, source}] for lines with no item that a rule covers and whose item is still in the master, at the supplier's sites."""
    supplier = _fold(invoice.get("supplier_name"))
    rules = all_rules(store) if supplier else []
    index = {(r["kind"], r["printed"]): r for r in rules if r["supplier"] == supplier}
    sites = ([site] if site else invoice_sites(source, invoice)) if rules else []
    fills = []
    for n, line in enumerate(invoice.get("lines") or []):
        if not index or str(line.get("item_id") or "").strip():
            continue
        rule = next((index[key] for key in _keys(line) if key in index), None)
        if rule is None:
            continue
        rows = source.items_by_parent(rule["item_id"])
        if any(str(r.get("SUPPLIER SITE") or "") in sites if sites else close_names(invoice.get("supplier_name"), r.get("SUPPLIER_NAME"))
               for r in rows):
            fills.append({"line": n, "rule": rule["id"], "item_id": rule["item_id"], "source": rule["source"]})
    return fills


def count_filled(store, applied):
    """Add a job's uses to each rule's lines_filled. Reading an invoice again counts again; each receipt lists its own."""
    if not applied:
        return
    with store.connection(True) as c:
        rules = [dict(r) for r in all_rules(store, c)]
        for rule in rules:
            rule["lines_filled"] = rule.get("lines_filled", 0) + sum(fill["rule"] == rule["id"] for fill in applied)
        store.set(STORE_KEY, rules, c)


def remove(store, rule_id):
    """Remove one rule. False when it is not there."""
    with store.connection(True) as c:
        rules = all_rules(store, c)
        kept = [r for r in rules if r["id"] != rule_id]
        if len(kept) == len(rules):
            return False
        store.set(STORE_KEY, kept, c)
        store.audit("ai_rule_removed", {"rule": rule_id, **_logged(next(r for r in rules if r["id"] == rule_id))}, c)
    return True
