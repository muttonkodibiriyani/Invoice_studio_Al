"""Compare a read with the truth: correct, blank or WRONG per field. WRONG with no flag on that field is the number that matters."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from rapidfuzz import fuzz

from .generator import CURRENCIES

HEADER_FIELDS = ("number", "supplier_name", "date", "po", "currency", "net", "tax")
LINE_FIELDS = ("sku", "gtin", "description", "uom", "qty", "price", "net_amount")
# Words a rules issue names when it concerns the field. A broad match on purpose: it can only undercount silent wrongs.
ISSUE_WORDS = {"number": ("document", "invoice number"), "supplier_name": ("supplier", "issuer"), "date": ("date",), "po": ("order",),
               "currency": ("currency",), "net": ("net", "total", "arithmetic", "gross", "sum"), "tax": ("tax", "vat", "arithmetic", "gross", "total")}
SYMBOLS = {v[2].upper().rstrip("."): k for k, v in CURRENCIES.items()}


def number(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def text_key(value):
    if value in (None, ""):
        return ""
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[\s.,'’_\-/]+", "", value)


def _po_key(value):
    # docs/FIELD_RULES.md: a leading LPO/PO word joined to the number is not part of po.
    return re.sub(r"^(lpo|po)", "", text_key(value))


def verdict_header(field, read, truth):
    """correct, blank or WRONG for one header field."""
    got, want = read.get(field), truth.get(field)
    if field == "tax" and truth.get("tax_printed") is False:
        return "correct" if number(got) in (None, Decimal(0)) else "WRONG"
    if field == "net" and truth.get("net_printed") is False:
        return "correct" if got in (None, "") or number(got) == number(want) else "WRONG"
    if field in ("net", "tax"):
        if got in (None, ""):
            return "correct" if want is None else "blank"
        return "correct" if number(got) == number(want) else "WRONG"
    if field == "date":
        want = want or None
        if got in (None, ""):
            # The app's own rule leaves an ambiguous date unparsed and keeps it as printed.
            if read.get("date_printed") and text_key(read["date_printed"]) == text_key(truth.get("date_printed")):
                return "correct"
            return "correct" if want is None and not truth.get("date_printed") else "blank"
        return "correct" if want and str(got)[:10] == want else ("correct" if want is None and text_key(got) == text_key(truth.get("date_printed")) else "WRONG")
    if got in (None, ""):
        return "correct" if want in (None, "") else "blank"
    if want in (None, ""):
        return "WRONG"
    if field == "currency":
        key = str(got).strip().upper().rstrip(".")
        return "correct" if key == str(want).upper() or SYMBOLS.get(key) == want else "WRONG"
    if field == "po":
        return "correct" if _po_key(got) == _po_key(want) else "WRONG"
    return "correct" if text_key(got) == text_key(want) else "WRONG"


def verdict_line(field, got, want):
    if field in ("qty", "price", "net_amount"):
        if got in (None, ""):
            return "correct" if want in (None, "") else "blank"
        return "correct" if number(got) == number(want) else "WRONG"
    if got in (None, ""):
        return "correct" if want in (None, "") else "blank"
    if want in (None, ""):
        return "WRONG"
    if field == "description":
        return "correct" if fuzz.ratio(text_key(got), text_key(want)) >= 90 else "WRONG"
    return "correct" if text_key(got) == text_key(want) else "WRONG"


def _pair_score(read, want):
    score = 0.0
    if read.get("sku") and text_key(read["sku"]) == text_key(want.get("sku")):
        score += 3
    if read.get("gtin") and text_key(read["gtin"]) == text_key(want.get("gtin")):
        score += 3
    if read.get("net_amount") not in (None, "") and number(read["net_amount"]) == number(want.get("net_amount")):
        score += 2
    if read.get("price") not in (None, "") and number(read["price"]) == number(want.get("price")):
        score += 1
    if read.get("qty") not in (None, "") and number(read["qty"]) == number(want.get("qty")):
        score += 1
    if read.get("description"):
        score += 2 * fuzz.ratio(text_key(read["description"]), text_key(want.get("description"))) / 100
    return score


def align(read_lines, truth_lines):
    """Pairs (read index, truth index), dropped truth rows and phantom read rows. Best pairs first, ties by position."""
    pairs = sorted(((_pair_score(r, t), -abs(i - j), i, j) for i, r in enumerate(read_lines) for j, t in enumerate(truth_lines)), reverse=True)
    seen_r, seen_t, out = set(), set(), []
    for score, _, i, j in pairs:
        if score >= 2.0 and i not in seen_r and j not in seen_t:
            out.append((i, j))
            seen_r.add(i)
            seen_t.add(j)
    dropped = [j for j in range(len(truth_lines)) if j not in seen_t]
    phantom = [i for i in range(len(read_lines)) if i not in seen_r]
    return sorted(out), dropped, phantom


def field_flagged(field, read_result, view):
    evidence = ((read_result.get("evidence") or {}).get("header") or {}).get(field) or {}
    if evidence.get("review"):
        return True
    view = view or {}
    key = {"supplier_name": "supplier", "number": "document"}.get(field, field)
    for name, cell in (view.get("fields") or {}).items():
        if (name == field or name == key) and cell.get("flagged"):
            return True
    words = ISSUE_WORDS.get(field, ())
    return any(any(w in (i.get("message") or "").lower() for w in words) for i in view.get("issues") or [] if not i.get("line"))


def lines_add_up(invoice):
    amounts = [number(x.get("net_amount")) for x in invoice.get("lines") or []]
    net = number(invoice.get("net"))
    if net is None or not amounts or any(a is None for a in amounts):
        return False
    return abs(sum(amounts) - net) <= Decimal("0.05")


def score_read(truth: dict, result: dict, view: dict | None = None) -> dict:
    """Verdicts for one read of one document, with each WRONG value and whether anything flagged it."""
    invoice = result.get("invoice") or {}
    out = {"fields": {}, "lines": {f: {"correct": 0, "blank": 0, "WRONG": 0, "silent": 0} for f in LINE_FIELDS}, "wrong": []}
    for field in HEADER_FIELDS:
        verdict = verdict_header(field, invoice, truth)
        flagged = verdict == "WRONG" and field_flagged(field, result, view)
        out["fields"][field] = {"verdict": verdict, "flagged": flagged}
        if verdict == "WRONG":
            out["wrong"].append({"field": field, "truth": truth.get(field), "read": invoice.get(field), "flagged": flagged})
    read_lines, truth_lines = invoice.get("lines") or [], truth.get("lines") or []
    pairs, dropped, phantom = align(read_lines, truth_lines)
    line_issues = {i.get("line") for i in (view or {}).get("issues") or [] if i.get("line")}
    for i, j in pairs:
        for field in LINE_FIELDS:
            verdict = verdict_line(field, read_lines[i].get(field), truth_lines[j].get(field))
            cell = out["lines"][field]
            cell[verdict] += 1
            if verdict == "WRONG" and i + 1 not in line_issues:
                cell["silent"] += 1
    for j in dropped:
        for field in LINE_FIELDS:
            if truth_lines[j].get(field) not in (None, ""):
                out["lines"][field]["blank"] += 1
    out["line_count"] = {"truth": len(truth_lines), "read": len(read_lines), "dropped": len(dropped), "phantom": len(phantom), "paired": len(pairs)}
    count_right = len(read_lines) == len(truth_lines) and not dropped and not phantom
    out["line_verdict"] = "correct" if count_right else "blank" if not read_lines else "WRONG"
    # A row lost while the rows still add up to the net: nothing in the arithmetic can catch it.
    out["dropped_passes_checks"] = bool(dropped and lines_add_up(invoice))
    out["ai_need"] = ((result.get("readers") or {}).get("ai") or {}).get("need")
    out["gaps"] = (result.get("readers") or {}).get("gaps") or []
    out["engine"] = result.get("selected_engine")
    out["pairs"], out["dropped"], out["phantom"] = pairs, dropped, phantom
    return out
