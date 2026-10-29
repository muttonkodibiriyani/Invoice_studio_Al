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
