"""Golden samples: the real local readers read each sample to its expected fields, and the AI is never called.

Nothing is stubbed but the AI reader, which records a call and so fails the test. The samples' expected fields
are samples/<name>.json. A reader a test needs that is not installed fails the test with its install hint; it
is never skipped. The scan tests need PaddleOCR, Docling and their models, so they carry the scan_readers
marker: `pytest -q` leaves them out, `pytest -q -m scan_readers` runs them (docs/GOLDEN_SAMPLES.md)."""
import json
import pathlib
import sys
import types
from decimal import Decimal

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import engines  # noqa: E402
from app.models import ProcessingOptions  # noqa: E402

AMOUNTS = {"net", "tax", "qty", "price"}


def needs(*readers):
    """Fail, with the install hint, when a reader the test reads with is not installed."""
    installed = {c["id"] for c in engines.capabilities() if c["installed"]}
    missing = [r for r in readers if r not in installed]
    if missing:
        pytest.fail(f"{', '.join(missing)} not installed: {engines._install_hint(missing[0])}")


@pytest.fixture
def read(tmp_path):
    """process() on a sample with the real readers; returns (result, the AI calls made)."""
    (tmp_path / "work").mkdir()

    def run(sample):
        calls = []

        def ai_reader(path, text, options):
            calls.append(path.name)
            raise AssertionError("the AI reader was called")

        path = sample if isinstance(sample, pathlib.Path) else ROOT / "samples" / sample
        result = engines.process(path, ProcessingOptions(engine="auto", model="m"),
                                 types.SimpleNamespace(root=tmp_path), ai_reader)
        return result, calls

    return run


def trace(result):
    return [(t["engine"], t["status"], t.get("reason", "")) for t in result["trace"]]


def assert_reads_expected(result, sample):
    expected = json.loads((ROOT / "samples" / sample).with_suffix(".json").read_text())
    invoice = result["invoice"]

    def same(field, mine, theirs):
        return Decimal(mine) == Decimal(theirs) if field in AMOUNTS and mine is not None else mine == theirs

    wrong = {f: invoice[f] for f, v in expected.items() if f != "lines" and not same(f, invoice[f], v)}
    assert not wrong, f"{sample}: fields read differently from the expected {wrong}; trace {trace(result)}"
    assert len(invoice["lines"]) == len(expected["lines"]), trace(result)
    for n, (line, want) in enumerate(zip(invoice["lines"], expected["lines"])):
        wrong = {f: line[f] for f, v in want.items() if not same(f, line[f], v)}
        assert not wrong, f"{sample} line {n + 1}: read differently from the expected {wrong}"


def assert_no_ai(result, calls):
    assert calls == [], f"the AI was asked to read a sample the local readers read: {trace(result)}"
    ai = result["readers"]["ai"]
    assert (ai["status"], ai["calls"]) == ("skipped", 0), ai
    assert "ai" not in result["readers"]["header"].values()


@pytest.mark.parametrize("sample", ["invoice.pdf", "invoice-2.pdf", "plain-invoice.pdf"])
def test_a_pdf_is_read_to_its_expected_fields_by_invoice2data_with_no_ai_call(read, sample):
    needs("invoice2data")
    result, calls = read(sample)
    assert_reads_expected(result, sample)
    assert_no_ai(result, calls)
    assert result["selected_engine"] == "invoice2data"


@pytest.mark.scan_readers
@pytest.mark.parametrize("sample", ["invoice-scan.png", "plain-invoice-scan.png"])
def test_the_scan_is_read_to_its_expected_fields_by_the_local_ocr_readers_with_no_ai_call(read, sample):
    needs("invoice2data", "paddleocr", "docling")
    result, calls = read(sample)
    assert_reads_expected(result, sample)
    assert_no_ai(result, calls)
    assert result["selected_engine"] in ("paddleocr", "docling")


def test_a_pdf_no_template_matches_is_offered_to_the_scan_readers_and_says_they_are_missing(
        read, monkeypatch, tmp_path):
    # The diagnosis's two exits (docs/AI_BOUNDARY.md, #52) on the real invoice2data: with no supplier template
    # the 250-character exit must not skip the scan readers, and a scan reader that is not installed must be
    # told on the job with its install hint, as the reason the AI ran.
    needs("invoice2data")
    monkeypatch.setattr(engines, "ROOT", tmp_path)  # no built-in templates
    real = engines.capabilities
    monkeypatch.setattr(engines, "capabilities", lambda: [
        {**c, "installed": c["installed"] and c["id"] == "invoice2data"} for c in real()])
    # A page over the 250 characters with no invoice on it: the template-free parsers now read the demo
    # invoice itself (C1), so the file the local readers cannot read is one that prints no invoice.
    from reportlab.pdfgen import canvas
    pdf = canvas.Canvas(str(tmp_path / "letter.pdf"))
    for n, line in enumerate((
            "Lumena Beauty Trading LLC", "Dear customer,",
            "Our warehouse will be closed for stock counting from the first to the third of next month.",
            "Orders placed in that window will be dispatched on the fourth, in the order they were received.",
            "Please contact your account manager with any question about a delivery in progress.",
            "SYNTHETIC DEMONSTRATION DATA. Not a real supplier letter.")):
        pdf.drawString(60, 780 - 16 * n, line)
    pdf.save()
    result, calls = read(tmp_path / "letter.pdf")
    hint = engines._install_hint("paddleocr")
    assert trace(result)[:3] == [
        ("invoice2data", "text_only", "No structured invoice fields were returned"),
        ("paddleocr", "unavailable", hint), ("docling", "unavailable", engines._install_hint("docling"))]
    assert result["readers"]["not_installed"] == [
        {"engine": "paddleocr", "hint": hint}, {"engine": "docling", "hint": engines._install_hint("docling")}]
    assert calls == ["letter.pdf"] and result["readers"]["ai"]["reason"].startswith("paddleocr, docling: ")
