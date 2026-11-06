"""Header fields of the real scans against their truth files, on the raw OCR reads kept in the private dir.

Needs the private corpus (INV_MASTER_GOLDEN_DIR), so it skips in CI. A failure names invoice fields, never their
values: nothing from a private invoice belongs in a test log."""
import json
import os
import pathlib
import types
from decimal import Decimal

import pytest

from app import engines, ocr_worker
from app.models import ProcessingOptions, uom_code

PRIVATE = pathlib.Path(os.environ.get("INV_MASTER_GOLDEN_DIR", "/nonexistent"))
READS = PRIVATE / "scan_vs_truth" / "reads"
PDFS = sorted(path for path in (PRIVATE / "invoices").glob("*.pdf") if path.with_suffix(".json").exists()) \
    if READS.is_dir() else []
if not PDFS:
    pytest.skip("needs the private corpus and its cached reads", allow_module_level=True)


def same(field, got, want):
    if field in ("net", "tax"):
        return got not in (None, "") and abs(Decimal(str(got)) - Decimal(str(want))) <= Decimal("0.01")
    words = lambda value: " ".join(str(value or "").upper().replace(".", " ").replace(",", " ").split())  # noqa: E731
    return words(got) == words(want)


@pytest.mark.parametrize("pdf", PDFS, ids=lambda pdf: f"invoice-{PDFS.index(pdf)}")
def test_header_and_lines_match_the_truth_file(pdf, monkeypatch, tmp_path):
    def cached(engine, path, root, language="en", extra=(), timeout=None):
        read = json.loads((READS / f"{pdf.stem}.{engine}.json").read_text())
        parsed, method = ocr_worker.structured_extract(
            read["text"], read["boxes"], ocr_worker.templates_from([engines.ROOT / "templates"]), read.get("tables") or [])
        if parsed:
            parsed["number"] = ocr_worker.restore_capital_i(parsed.get("number"))
        return {**read, "invoice": parsed, "extraction_method": method}

    def no_ai(*_):
        raise AssertionError("the AI must not be called")

    monkeypatch.setattr(engines, "local_read", cached)
    (tmp_path / "work").mkdir()
    options = ProcessingOptions(engine="auto", prefer_native_text=True, ai_fallback=False, provider="openai", model="m",
                                language="en")
    invoice = engines.process(pdf, options, types.SimpleNamespace(root=tmp_path), no_ai)["invoice"]
    truth = json.loads(pdf.with_suffix(".json").read_text())
    gaps = [field for field in ("number", "supplier_name", "date", "currency", "net", "tax")
            if not same(field, invoice.get(field), truth.get(field))]
    if truth.get("po"):
        gaps += [] if same("po", invoice.get("po"), truth["po"]) else ["po"]
    if len(invoice["lines"]) != len(truth["lines"]):
        gaps.append("line count")
    gaps += [f"line {n} uom" for n, (line, want) in enumerate(zip(invoice["lines"], truth["lines"]), 1)
             if uom_code(line.get("uom")) != uom_code(want.get("uom"))]
    assert not gaps
