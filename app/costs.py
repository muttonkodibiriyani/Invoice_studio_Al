"""What an extraction cost: the AI tokens it used, priced, and rolled up by day.

Every provider returns its token counts with the read, and those counts already sit in the
job's trace. This module is the only place that turns them into money: one published price
table, the prices recorded beside every figure it produces, and a daily rollup so a jump in
spend is visible without waiting for a billing export.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal, ROUND_HALF_UP
from statistics import median

# Dollars per million tokens as (input, output, cached input), by the date each price starts.
# Vertex AI list prices read on 23 Oct 2026; Google has published the rise that takes effect
# on 1 Jan 2027, so a read priced in January is priced at January's rate and not today's.
PRICES = {
    "gemini-3.7-flash": (("2026-01-01", "0.75", "3.75", "0.075"),
                         ("2027-01-01", "1.50", "7.50", "0.15")),
}
CENT = Decimal("0.000001")


def _overrides():
    """Prices from the environment, for a published change that must not wait for a deploy.

    INV_STUDIO_AI_PRICES={"gemini-3.7-flash": {"input": 1.5, "output": 7.5, "cached": 0.15}}
    """
    raw = os.getenv("INV_STUDIO_AI_PRICES", "").strip()
    if not raw:
        return {}
    try:
        return {str(model): entry for model, entry in json.loads(raw).items()}
    except (ValueError, AttributeError):
        return {}


def price_of(model, day):
    """Dollars per million (input, output, cached) for a model on a day, or None if unpriced.

    An unknown model is not guessed at: its tokens are still counted, and the cost is reported
    as unpriced rather than as zero.
    """
    name = str(model or "")
    override = _overrides().get(name)
    if override:
        return tuple(Decimal(str(override.get(key, 0))) for key in ("input", "output", "cached"))
    tiers = next((v for k, v in PRICES.items() if name.startswith(k)), None)
    if not tiers:
        return None
    starts, *rates = max((t for t in tiers if t[0] <= str(day)), default=tiers[0])
    return tuple(Decimal(rate) for rate in rates)


def _counts(usage):
    """(input, output, cached) of one provider usage record, or None when it carries no counts.

    Vertex reports promptTokenCount / candidatesTokenCount / cachedContentTokenCount, with
    thinking separate. The OpenAI family (OpenAI, ChatGPT, Azure AI Foundry) reports
    input_tokens / output_tokens with the cached part inside input_tokens_details and
    reasoning already inside output_tokens; Anthropic reports input_tokens that exclude its
    cache_read_input_tokens.
    """
    if "promptTokenCount" in usage or "candidatesTokenCount" in usage:
        prompt = int(usage.get("promptTokenCount") or 0)
        if not prompt and not usage.get("candidatesTokenCount"):
            return None
        hit = int(usage.get("cachedContentTokenCount") or 0)
        return (max(0, prompt - hit),
                int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0), hit)
    if "input_tokens" in usage or "output_tokens" in usage:
        prompt = int(usage.get("input_tokens") or 0)
        output = int(usage.get("output_tokens") or 0)
        if not prompt and not output:
            return None
        details = usage.get("input_tokens_details")
        hit = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
        if hit:
            return (max(0, prompt - hit), output, hit)
        return (prompt, output, int(usage.get("cache_read_input_tokens") or 0))
    return None


def usage_of(trace):
    """The AI token counts of one extraction, summed over its calls.

    Thinking tokens are billed as output, and cached input is billed at the cached rate, so
    the plain input count has the cached part taken out of it.
    """
    calls = input_tokens = output_tokens = cached = 0
    for entry in trace or []:
        usage = entry.get("usage") if isinstance(entry, dict) else None
        if not isinstance(usage, dict):
            continue
        counts = _counts(usage)
        if counts is None:
            continue
        calls += 1
        input_tokens += counts[0]
        output_tokens += counts[1]
        cached += counts[2]
    return {"calls": calls, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cached_tokens": cached}


def model_of(trace):
    """The model a trace says did the reading, for a record written before costs were kept."""
    for entry in trace or []:
        if isinstance(entry, dict) and entry.get("model") and entry.get("usage"):
            return str(entry["model"])
    return ""


def record(trace, model, day):
    """The cost record kept with an extraction: what it used, what it was priced at, what it cost.

    The price is written down beside the figure so a later price change cannot rewrite history.
    """
    usage = usage_of(trace)
    rates = price_of(model, day)
    entry = {**usage, "model": str(model or ""), "day": str(day)}
    if not usage["calls"]:
        # A read the local engines finished on their own: nothing was sent to a provider, so it
        # cost nothing. That is a priced zero, not an unknown.
        return {**entry, "usd": "0.000000", "priced": True}
    if rates is None:
        return {**entry, "usd": None, "priced": False}
    rate_in, rate_out, rate_cached = rates
    usd = (usage["input_tokens"] * rate_in + usage["output_tokens"] * rate_out
           + usage["cached_tokens"] * rate_cached) / Decimal(1_000_000)
    return {**entry, "usd": str(usd.quantize(CENT, rounding=ROUND_HALF_UP)), "priced": True,
            "price_per_million": {"input": str(rate_in), "output": str(rate_out), "cached": str(rate_cached)}}


def _money(value):
    try:
        return Decimal(str(value))
    except (TypeError, ValueError):
        return Decimal(0)


def summarise(entries, *, days=30, spike_factor=3, spike_floor="1.00"):
    """Daily and month-to-date spend from cost records, newest day first.

    `entries` are {"day": "2026-10-23", "cost": <record or None>} - one per extraction. A day
    is flagged when it spends more than `spike_factor` times the median of the other days in
    the window and more than `spike_floor`, which is what "well above normal" means here.
    """
    per_day = {}
    for entry in entries:
        day = str(entry.get("day") or "")[:10]
        if not day:
            continue
        cost = entry.get("cost") or {}
        bucket = per_day.setdefault(day, {"day": day, "invoices": 0, "ai_calls": 0,
                                          "input_tokens": 0, "output_tokens": 0,
                                          "cached_tokens": 0, "usd": Decimal(0), "unpriced": 0})
        bucket["invoices"] += 1
        bucket["ai_calls"] += int(cost.get("calls") or 0)
        for field in ("input_tokens", "output_tokens", "cached_tokens"):
            bucket[field] += int(cost.get(field) or 0)
        if cost.get("usd") is None:
            bucket["unpriced"] += int(cost.get("calls") or 0)
        else:
            bucket["usd"] += _money(cost.get("usd"))
    ordered = [per_day[day] for day in sorted(per_day, reverse=True)][:days]
    spends = [row["usd"] for row in ordered]
    normal = median(spends) if spends else Decimal(0)
    floor = Decimal(spike_floor)
    for row in ordered:
        row["spike"] = bool(row["usd"] > floor and normal > 0 and row["usd"] > normal * spike_factor)
        row["usd"] = str(row["usd"].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    month = (ordered[0]["day"][:7] if ordered else "")
    month_to_date = sum((_money(row["usd"]) for row in ordered if row["day"][:7] == month), Decimal(0))
    return {"days": ordered, "median_daily_usd": str(normal.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "month": month, "month_to_date_usd": str(month_to_date.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "invoices": sum(row["invoices"] for row in ordered),
            "ai_calls": sum(row["ai_calls"] for row in ordered),
            "spikes": [row["day"] for row in ordered if row["spike"]]}
