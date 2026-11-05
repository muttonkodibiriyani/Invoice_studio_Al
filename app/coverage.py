"""V7.1 match coverage (rules workbook Coverage_Tiers): the share of invoice lines whose Item the rules found decides
the tier, and the tier decides export. A reviewer-typed Item fills a gap but is not a match: below the last fallback
the missing items go into the item master and Update item master re-runs the rules (U8.1)."""
from decimal import ROUND_FLOOR, Decimal


def _matched(cell):
    evidence = (cell or {}).get("evidence") or []
    return bool((cell or {}).get("value")) and any(e.get("kind") != "owner_entry" for e in evidence)


def coverage(view, tiers):
    """``tiers`` are RuleSet.coverage_tiers(): ``name``, ``low`` (inclusive) and ``high`` (exclusive, None: no top) in
    percent, ``export_mode`` 'yes' | 'after_fill' | 'no', ``prompt`` id or None.

    ``{matched, total, pct, tier, export, low, prompt, missing[]}`` for a rules view; ``missing`` lists every line the
    rules did not match, ``filled`` when the reviewer gave it an Item."""
    lines = (view or {}).get("lines") or []
    missing = [{"line": l["line"], "filled": bool(l["cells"].get("Item", {}).get("value"))}
               for l in lines if not _matched(l["cells"].get("Item"))]
    total = len(lines); matched = total - len(missing)
    pct = Decimal(100) * matched / total if total else Decimal(0)  # unrounded, so 94.999 is never tier 95
    tier = next((t for t in tiers if t.low <= pct and (t.high is None or pct < t.high)), None)
    if tier is None:raise ValueError(f"No Coverage_Tiers row covers {pct}%")
    # A fallback under another fallback is flagged as low coverage (Second fallback).
    low = tier.export_mode == "after_fill" and any(t.export_mode == "after_fill" and t.low > tier.low for t in tiers)
    return {"matched": matched, "total": total, "pct": str(pct.quantize(Decimal("0.01"), ROUND_FLOOR)), "tier": tier.name,
            "export": tier.export_mode, "low": low, "prompt": tier.prompt, "missing": missing}


def _lines(numbers):
    return ("line " if len(numbers) == 1 else "lines ") + ", ".join(map(str, numbers))


def refusal(cov):
    """Why export must wait, or None when the coverage allows it."""
    if cov["export"] == "yes":return None
    if not cov["total"]:return "The invoice has no lines to export."
    share = f"{cov['matched']} of {cov['total']} lines ({cov['pct']}%)"
    if cov["export"] == "no":
        return f"Coverage {share}. Add the missing items ({_lines([m['line'] for m in cov['missing']])}) to the item master, then Update item master."
    gaps = [m["line"] for m in cov["missing"] if not m["filled"]]
    if not gaps:return None
    return f"{'Low coverage: ' if cov['low'] else 'Coverage '}{share}. Fill or confirm {_lines(gaps)} before export."
