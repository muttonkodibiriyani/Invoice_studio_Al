"""What a read cost: pricing the token counts a trace carries, and the daily rollup.

The figures here are the measured ones from the real invoice set (a median read of 3,940
input and 1,699 output tokens), so a price table edited by accident shows up as a changed
cost per invoice rather than as a quietly different monthly total.
"""

from app.costs import model_of, price_of, record, summarise, usage_of

MEDIAN_READ = [{"engine": "vertex", "model": "gemini-3.7-flash",
                "usage": {"promptTokenCount": 3940, "candidatesTokenCount": 1699}}]


def test_the_median_read_is_priced_at_a_bit_under_a_cent():
    cost = record(MEDIAN_READ, "gemini-3.7-flash", "2026-10-23")
    assert cost["usd"] == "0.009326"
    assert cost["calls"] == 1 and cost["input_tokens"] == 3940 and cost["output_tokens"] == 1699
    # The price is written down with the figure, so a later price change cannot rewrite history.
    assert cost["price_per_million"] == {"input": "0.75", "output": "3.75", "cached": "0.075"}


def test_a_read_in_january_is_priced_at_januarys_published_rate():
    assert price_of("gemini-3.7-flash", "2026-12-31") == price_of("gemini-3.7-flash", "2026-10-23")
    assert record(MEDIAN_READ, "gemini-3.7-flash", "2027-01-01")["usd"] == "0.018653"


def test_an_unpriced_model_counts_its_tokens_and_says_it_is_unpriced():
    """A model with no published price in the table must not report a cost of zero."""
    cost = record(MEDIAN_READ, "some-new-model", "2026-10-23")
    assert cost["priced"] is False and cost["usd"] is None
    assert cost["input_tokens"] == 3940


def test_cached_input_and_thinking_tokens_are_counted_where_they_are_billed():
    usage = usage_of([{"usage": {"promptTokenCount": 5000, "cachedContentTokenCount": 2000,
                                 "candidatesTokenCount": 1000, "thoughtsTokenCount": 500}}])
    assert usage == {"calls": 1, "input_tokens": 3000, "output_tokens": 1500, "cached_tokens": 2000}


def test_a_trace_without_token_counts_is_not_counted_as_a_call():
    assert usage_of([{"engine": "paddleocr", "status": "extracted"}])["calls"] == 0
    assert usage_of(None)["calls"] == 0


def test_a_read_the_local_engines_finished_alone_costs_a_priced_nothing():
    """No provider was called, so the cost is zero and known, not unpriced."""
    cost = record([{"engine": "paddleocr", "status": "extracted"}], "gemini-3.7-flash", "2026-10-23")
    assert cost["usd"] == "0.000000" and cost["priced"] is True and cost["calls"] == 0


def test_the_model_is_taken_from_the_trace_for_a_record_written_before_costs_were_kept():
    assert model_of(MEDIAN_READ) == "gemini-3.7-flash"
    assert model_of([{"engine": "native_pdf_text"}]) == ""


def test_the_rollup_totals_each_day_and_the_month_so_far():
    cost = record(MEDIAN_READ, "gemini-3.7-flash", "2026-10-23")
    entries = [{"day": "2026-10-23", "cost": cost}] * 3 + [{"day": "2026-10-22", "cost": cost}]
    summary = summarise(entries)
    assert [day["day"] for day in summary["days"]] == ["2026-10-23", "2026-10-22"]
    assert summary["days"][0]["invoices"] == 3 and summary["days"][0]["ai_calls"] == 3
    assert summary["days"][0]["usd"] == "0.0280"
    assert summary["month"] == "2026-10" and summary["month_to_date_usd"] == "0.0373"
    assert summary["invoices"] == 4


def test_a_day_well_above_normal_is_flagged_and_an_ordinary_one_is_not():
    """What 'well above normal' means: more than three times the median day, and more than a dollar."""
    quiet = record([{"model": "gemini-3.7-flash",
                     "usage": {"promptTokenCount": 4000, "candidatesTokenCount": 150000}}],
                   "gemini-3.7-flash", "2026-10-20")
    entries = [{"day": f"2026-10-{day:02d}", "cost": quiet} for day in (18, 19, 20, 21)]
    assert summarise(entries)["spikes"] == []
    loud = [{"day": "2026-10-22", "cost": quiet} for _ in range(8)]
    summary = summarise(entries + loud)
    assert summary["spikes"] == ["2026-10-22"]
    assert next(day for day in summary["days"] if day["day"] == "2026-10-21")["spike"] is False


def test_a_price_change_can_be_applied_without_a_deploy(monkeypatch):
    monkeypatch.setenv("INV_STUDIO_AI_PRICES",
                       '{"gemini-3.7-flash": {"input": 1.5, "output": 7.5, "cached": 0.15}}')
    assert record(MEDIAN_READ, "gemini-3.7-flash", "2026-10-23")["usd"] == "0.018653"


def test_the_openai_family_usage_shape_is_counted_where_it_is_billed():
    """OpenAI, ChatGPT and Azure AI Foundry answer input_tokens/output_tokens, cached inside the input."""
    usage = usage_of([{"engine": "azure", "model": "invoice-reader",
                       "usage": {"input_tokens": 5000, "input_tokens_details": {"cached_tokens": 2000},
                                 "output_tokens": 1500, "output_tokens_details": {"reasoning_tokens": 500}}}])
    assert usage == {"calls": 1, "input_tokens": 3000, "output_tokens": 1500, "cached_tokens": 2000}
    # Anthropic's input count already excludes what it read from its cache.
    usage = usage_of([{"usage": {"input_tokens": 3000, "output_tokens": 100, "cache_read_input_tokens": 2000}}])
    assert usage == {"calls": 1, "input_tokens": 3000, "output_tokens": 100, "cached_tokens": 2000}
    assert usage_of([{"usage": {"input_tokens": 0, "output_tokens": 0}}])["calls"] == 0
    # A Vertex record is read exactly as before.
    assert usage_of(MEDIAN_READ) == {"calls": 1, "input_tokens": 3940, "output_tokens": 1699, "cached_tokens": 0}
