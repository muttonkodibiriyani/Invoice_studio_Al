"""The local readers read an invoice before the AI is asked: a PDF no supplier template matches still goes
through the installed layout and OCR readers, and a reader the file needed but that is not installed is told
on the job with its install hint, as the reason the AI ran."""
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import engines  # noqa: E402
from app.models import Invoice, ProcessingOptions  # noqa: E402

INVOICE = json.loads((ROOT / "samples" / "invoice.json").read_text())
# The demo PDF's text without a template match: long enough for the 250-character exit.
TEXT = "Lumena Beauty Trading LLC\nInvoice DEMO-2026-001\n" + "ITEMS BEGIN Lumena serum 30 ml 10 60.00\n" * 8


@pytest.fixture
def chain(monkeypatch, tmp_path):
    """Run process() with stub readers; returns (result, order of reader and AI calls)."""
    (tmp_path / "work").mkdir()

    def run(sample, installed=("invoice2data", "paddleocr", "docling"), reads=None, ai=INVOICE, evidence=None,
            **options):
        reads = reads or {}
        monkeypatch.delenv("INV_STUDIO_AI_CROSS_CHECK", raising=False)
        calls = []
        monkeypatch.setattr(engines, "capabilities", lambda: [
            {"id": e, "installed": e in installed, "models_blocked": False, "version": "1", "note": ""}
            for e in ("invoice2data", "paddleocr", "docling")])

        def local_read(engine, path, root, language="en", extra=()):
            calls.append(engine)
            # A scan has no text layer: only the OCR readers read its text.
            text = "" if engine == "invoice2data" and path.suffix == ".png" else TEXT
            return {"text": text, "invoice": reads.get(engine), "boxes": [], "tables": []}

        def ai_reader(path, text, options):
            calls.append("ai")
            return Invoice.model_validate(ai), {"evidence": evidence or {}}

        monkeypatch.setattr(engines, "local_read", local_read)
        result = engines.process(ROOT / "samples" / sample, ProcessingOptions(**{"engine": "auto", "model": "m", **options}),
                                 types.SimpleNamespace(root=tmp_path), ai_reader)
        return result, calls

    return run


def statuses(result):
    return [(t["engine"], t["status"]) for t in result["trace"]]


def test_a_pdf_no_template_matches_is_read_by_the_ocr_readers_before_any_ai_call(chain):
    result, calls = chain("invoice.pdf", reads={"paddleocr": INVOICE})
    assert calls == ["invoice2data", "paddleocr"], "the 250-character exit must not hand an unmatched PDF to the AI"
    assert statuses(result)[:3] == [("invoice2data", "text_only"), ("paddleocr", "extracted"),
                                    ("docling", "skipped")]
    assert result["selected_engine"] == "paddleocr"
    assert result["readers"]["ai"]["calls"] == 0


def test_a_pdf_a_template_matches_still_skips_the_ocr_readers(chain):
    result, calls = chain("invoice.pdf", reads={"invoice2data": INVOICE})
    assert calls == ["invoice2data"]
    assert ("paddleocr", "skipped") in statuses(result)


def test_a_scan_is_read_by_the_local_ocr_reader_before_any_ai_call(chain):
    result, calls = chain("invoice-scan.png", reads={"paddleocr": INVOICE})
    assert calls[:2] == ["invoice2data", "paddleocr"] and "ai" not in calls
    assert result["selected_engine"] == "paddleocr"


def test_a_missing_reader_is_told_on_the_job_with_its_install_hint_as_why_the_ai_ran(chain):
    result, calls = chain("invoice-scan.png", installed=("invoice2data",))
    hint = engines._install_hint("paddleocr")
    assert calls == ["invoice2data", "ai"]
    trace = {t["engine"]: t for t in result["trace"]}
    assert trace["paddleocr"]["status"] == "unavailable" and trace["paddleocr"]["reason"] == hint
    assert trace["openai"]["readers_not_installed"] == ["paddleocr", "docling"]
    ai = result["readers"]["ai"]
    assert ai["need"] == "fallback" and ai["calls"] == 1
    assert ai["reason"].startswith("paddleocr, docling: not installed. Run ")
    assert result["readers"]["not_installed"] == [
        {"engine": "paddleocr", "hint": hint}, {"engine": "docling", "hint": engines._install_hint("docling")}]


def test_a_reader_the_file_did_not_need_is_not_reported_missing(chain):
    result, _ = chain("invoice.pdf", installed=("invoice2data",), reads={"invoice2data": INVOICE})
    assert result["readers"]["not_installed"] == []
    assert "not installed" not in result["readers"]["ai"]["reason"]


def test_a_read_without_its_net_does_not_stop_the_chain_before_the_other_local_readers(chain):
    short = {**INVOICE, "net": None}
    result, calls = chain("invoice.pdf", reads={"invoice2data": short, "paddleocr": short, "docling": INVOICE})
    assert calls == ["invoice2data", "paddleocr", "docling"]
    assert result["selected_engine"] == "docling" and result["readers"]["ai"]["calls"] == 0


# The AI boundary (docs/AI_BOUNDARY.md): when the AI may run after the local readers, and what it may change.
PRICED = {**INVOICE, "lines": [{**line, "net_amount": net} for line, net in zip(INVOICE["lines"], ("600", "200"))]}


def local_readers(result):
    return {r for r in [*result["readers"]["header"].values(),
                        *(v for line in result["readers"]["lines"] for v in line.values())]}


def test_a_complete_local_read_is_never_sent_to_the_ai_unless_cross_check_is_on(chain):
    result, calls = chain("invoice.pdf", reads={"invoice2data": INVOICE})
    assert "ai" not in calls
    ai = result["readers"]["ai"]
    assert (ai["need"], ai["status"], ai["calls"]) == ("cross_check", "skipped", 0)
    assert local_readers(result) == {"invoice2data"}, "every field names the engine that read it"
    result, calls = chain("invoice.pdf", reads={"invoice2data": INVOICE}, ai_cross_check=True)
    assert calls.count("ai") == 1 and result["readers"]["ai"]["status"] == "cross_check"


def test_gap_fill_fills_only_the_empty_field_and_names_the_ai_as_its_reader(chain):
    result, calls = chain("invoice.pdf", reads={"invoice2data": {**INVOICE, "date": None}})
    assert calls == ["invoice2data", "ai"]
    ai = result["readers"]["ai"]
    assert ai["status"] == "gap_fill" and ai["reason"] == engines.AI_REASONS["gap_fill"]
    assert result["readers"]["header"]["date"] == "ai" and result["readers"]["header"]["net"] == "invoice2data"


@pytest.mark.parametrize("engine", ["auto", "invoice2data", "paddleocr", "docling"])
@pytest.mark.parametrize("sample,read", [
    ("invoice.pdf", None),  # nothing read: fallback
    ("invoice.pdf", {**INVOICE, "date": None}),  # gap fill
    ("invoice.pdf", INVOICE),  # complete, with cross-check on
    ("invoice-scan.png", {**INVOICE, "net": "8.0"}),  # weak scan read
])
def test_with_ai_fallback_off_no_path_calls_the_ai(chain, engine, sample, read):
    reads = {e: read for e in ("invoice2data", "paddleocr", "docling")} if read else {}
    result, calls = chain(sample, reads=reads, engine=engine, ai_fallback=False, ai_cross_check=True)
    assert "ai" not in calls
    assert result["readers"]["ai"]["status"] == "off" and result["readers"]["ai"]["calls"] == 0


def test_the_ai_selected_as_the_reader_reads_the_whole_invoice_and_says_so(chain):
    result, calls = chain("invoice.pdf", engine="ai", ai_fallback=False)
    assert calls == ["ai"], "engine=ai is the user's choice of the AI as reader, not a fallback"
    assert result["readers"]["ai"]["reason"] == engines.AI_REASONS["selected"]
    assert local_readers(result) == {"ai"}


def test_no_local_read_makes_the_ai_the_whole_invoice_and_the_reason_says_so(chain):
    result, _ = chain("invoice.pdf", installed=("invoice2data",))
    assert result["readers"]["ai"]["reason"].endswith(engines.AI_REASONS["whole"])
    assert local_readers(result) == {"ai"}
    # On a scan the AI's read must pass its own checks (printed line nets that sum to its net).
    result, _ = chain("invoice-scan.png", installed=("invoice2data",), ai=PRICED)
    assert "replaced the whole invoice" in result["readers"]["ai"]["reason"]
    assert local_readers(result) == {"ai"}


def test_a_weak_scan_read_loses_only_the_field_the_ai_reads_differently(chain):
    # C2 of the diagnosis: a stray OCR token makes the net 8.0; the lines and other header fields are right.
    result, calls = chain("invoice-scan.png", installed=("invoice2data", "paddleocr"),
                          reads={"paddleocr": {**PRICED, "net": "8.0"}}, ai=PRICED,
                          evidence={"net": {"quote": "Net total: 800.00", "page": 1}})
    assert calls == ["invoice2data", "paddleocr", "ai"]
    assert result["invoice"]["net"] == "800" and result["readers"]["header"]["net"] == "ai"
    assert result["selected_engine"] == "paddleocr"
    assert local_readers(result) == {"paddleocr", "ai"}
    assert all(set(line.values()) == {"paddleocr"} for line in result["readers"]["lines"])
    reason = result["readers"]["ai"]["reason"]
    assert reason.endswith("replaced only the fields that are empty or disagree: net"), reason


def test_a_weak_scan_read_that_passes_the_basic_checks_keeps_every_value_it_read(chain):
    # No item codes makes the scan read weak, but it reconciles: the AI fills the codes and replaces nothing.
    uncoded = {**INVOICE, "lines": [{**line, "sku": None, "gtin": None} for line in INVOICE["lines"]]}
    result, _ = chain("invoice-scan.png", reads={"paddleocr": uncoded}, ai={**INVOICE, "net": "900"})
    ai = result["readers"]["ai"]
    assert ai["status"] == "gap_fill" and ai["reason"] == engines.AI_REASONS["gap_fill"]
    assert result["invoice"]["net"] == "800" and result["readers"]["header"]["net"] == "paddleocr"
    assert [line["sku"] for line in result["readers"]["lines"]] == ["ai", "ai"]
    assert result["evidence"]["header"]["net"]["review"]["other_value"] == "900"


def _blank_quantity(invoice):
    return {**invoice, "lines": [{**invoice["lines"][0], "qty": None}, *invoice["lines"][1:]]}


def test_a_read_with_a_blank_quantity_does_not_stop_the_chain_and_keeps_its_ai_mode(chain):
    # Nothing checks a line with no quantity, so its sum proves nothing: the next reader still runs. The AI mode
    # stays gap fill, because stopping the chain is a separate question from whether the read can stand.
    result, calls = chain("invoice-scan.png", reads={"paddleocr": _blank_quantity(INVOICE)})
    assert "docling" in calls
    ai = result["readers"]["ai"]
    assert ai["need"] == "gap_fill" and ai["status"] == "gap_fill"


def test_a_complete_later_read_takes_over_from_a_blank_quantity(chain):
    result, _ = chain("invoice-scan.png", reads={"paddleocr": _blank_quantity(INVOICE), "docling": INVOICE},
                      ai_fallback=False)
    assert result["selected_engine"] == "docling"


def test_on_a_tied_score_the_read_that_passes_the_basic_checks_is_kept(chain):
    # Each read leaves one gap: the first has no number, the second no currency. Only the second names the invoice.
    unnamed = {**INVOICE, "number": None}
    named = {**INVOICE, "currency": None, "po": "from-the-second-read"}
    result, calls = chain("invoice-scan.png", reads={"paddleocr": unnamed, "docling": named}, ai_fallback=False)
    assert "docling" in calls
    assert result["selected_engine"] == "docling"
    assert result["invoice"]["number"] == INVOICE["number"]


def test_on_a_tied_score_the_first_read_stays_when_neither_is_better(chain):
    # Both fail the same way: the order decides, as it always has.
    first, second = {**INVOICE, "number": None, "po": "first"}, {**INVOICE, "number": None, "po": "second"}
    result, _ = chain("invoice-scan.png", reads={"paddleocr": first, "docling": second}, ai_fallback=False)
    assert result["selected_engine"] == "paddleocr"


def _zero_value(invoice, lines):
    return {**invoice, "net": "0", "tax": "0",
            "lines": [{**line, "price": "0", "net_amount": "0", "tax_amount": "0"} for line in invoice["lines"][:lines]]}


def test_a_zero_value_read_does_not_end_the_chain_and_the_read_with_more_rows_is_kept(chain):
    # Lines summing to a net of 0 cannot show a row is missing: only the count of rows read can.
    rows = len(INVOICE["lines"])
    short, full = _zero_value(INVOICE, rows - 1), {**_zero_value(INVOICE, rows), "date": None}
    result, calls = chain("invoice-scan.png", reads={"paddleocr": short, "docling": full}, ai_fallback=False)
    assert "docling" in calls
    assert result["selected_engine"] == "docling"
    assert len(result["invoice"]["lines"]) == rows


def test_a_later_zero_value_read_with_fewer_rows_does_not_take_over(chain):
    rows = len(INVOICE["lines"])
    full, short = {**_zero_value(INVOICE, rows), "date": None}, _zero_value(INVOICE, rows - 1)
    result, _ = chain("invoice-scan.png", reads={"paddleocr": full, "docling": short}, ai_fallback=False)
    assert result["selected_engine"] == "paddleocr"
    assert len(result["invoice"]["lines"]) == rows


def _junk_row(read):
    return {**read, "lines": [*read["lines"], {"description": "Total lines", "qty": None, "price": None}]}


def _no_codes(read):
    return {**read, "lines": [{**line, "sku": None, "gtin": None} for line in read["lines"]]}


def test_a_later_read_with_one_extra_junk_row_does_not_take_over(chain):
    full = _zero_value(INVOICE, len(INVOICE["lines"]))
    result, _ = chain("invoice-scan.png", reads={"paddleocr": full, "docling": _junk_row(full)}, ai_fallback=False)
    assert result["selected_engine"] == "paddleocr"
    assert len(result["invoice"]["lines"]) == len(full["lines"])


def test_a_junk_row_in_the_first_read_does_not_block_a_better_later_read(chain):
    rows = len(INVOICE["lines"])
    short, full = _junk_row(_zero_value(INVOICE, rows - 1)), {**_zero_value(INVOICE, rows), "date": None}
    result, _ = chain("invoice-scan.png", reads={"paddleocr": short, "docling": full}, ai_fallback=False)
    assert result["selected_engine"] == "docling"


def test_more_priced_rows_do_not_win_when_they_lose_the_codes(chain):
    rows = len(INVOICE["lines"])
    short, full = _zero_value(INVOICE, rows - 1), {**_no_codes(_zero_value(INVOICE, rows)), "date": None}
    result, _ = chain("invoice-scan.png", reads={"paddleocr": short, "docling": full}, ai_fallback=False)
    assert result["selected_engine"] == "paddleocr"


def test_the_count_of_rows_decides_only_between_two_zero_value_reads():
    from decimal import Decimal
    from app.engines import _rows_ahead
    from app.models import Invoice

    def invoice(net, rows):
        return Invoice.model_validate({**INVOICE, "net": net, "tax": "0", "lines": [
            {**INVOICE["lines"][0], "price": "0" if net == "0" else "1", "net_amount": None} for _ in range(rows)]})

    assert _rows_ahead(invoice("0", 3), invoice("0", 2)) == 1
    assert _rows_ahead(invoice("0", 2), invoice("0", 3)) == -1
    assert _rows_ahead(invoice("5", 3), invoice("0", 2)) == 0
    assert _rows_ahead(invoice("0", 3), invoice("5", 2)) == 0
    assert _rows_ahead(invoice("5", 3), invoice("5", 2)) == 0
    assert Decimal("0") == invoice("0", 1).net


def test_zero_value_reads_count_only_coded_rows_when_the_earlier_read_has_codes():
    from app.engines import _rows_ahead
    from app.models import Invoice

    def invoice(coded, uncoded):
        line = {**INVOICE["lines"][0], "price": "0", "net_amount": None}
        return Invoice.model_validate({**INVOICE, "net": "0", "tax": "0", "lines": [
            *(line for _ in range(coded)), *({**line, "sku": None, "gtin": None} for _ in range(uncoded))]})

    assert _rows_ahead(invoice(2, 3), invoice(2, 0)) == 0  # more rows, no more codes
    assert _rows_ahead(invoice(3, 0), invoice(2, 0)) == 1
    assert _rows_ahead(invoice(1, 5), invoice(2, 0)) == -1  # more rows that lose a code
    assert _rows_ahead(invoice(0, 3), invoice(0, 2)) == 1  # no codes yet: the priced rows decide
    assert _rows_ahead(invoice(2, 0), invoice(0, 3)) == -1  # likewise: the earlier read has none to count
    assert _rows_ahead(invoice(1, 0), invoice(1, 1)) == -1  # the coded rows tie, but it read fewer rows


def test_a_later_zero_value_read_with_fewer_rows_never_wins_on_a_tie_of_coded_rows(chain):
    two = _zero_value(INVOICE, 2)
    first = {**two, "lines": [two["lines"][0], {**two["lines"][1], "sku": None, "gtin": None}]}
    result, _ = chain("invoice-scan.png", reads={"paddleocr": first, "docling": _zero_value(INVOICE, 1)}, ai_fallback=False)
    assert result["selected_engine"] == "paddleocr" and len(result["invoice"]["lines"]) == 2
