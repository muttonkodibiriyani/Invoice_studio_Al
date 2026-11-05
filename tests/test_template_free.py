"""A plain line-item invoice that no supplier template matches is read by the local parsers alone, as a
digital PDF and as a scan, and the demo layout reads without its template (C1 of the reader diagnosis).
The scans are PaddleOCR's recorded text and word boxes, so the tests run where PaddleOCR is not installed."""
import json
import pathlib
import types
from decimal import Decimal

import pytest

from app import engines
from app.models import Invoice, ProcessingOptions
from app.ocr_worker import digital, structured_extract, template_extract, templates_from

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
FIXTURES = ROOT / "tests" / "fixtures"


def pdf_read(name):
    text, boxes, tables = digital(SAMPLES / name, with_tables=True)
    return text, boxes, tables


def scan_read(name):
    recorded = json.loads((FIXTURES / f"{name}.paddleocr.json").read_text())
    return recorded["text"], recorded["boxes"], []


def read(source):
    """Every template-free parser in its order: no supplier template is loaded."""
    invoice, method = structured_extract(source[0], source[1], [], source[2])
    assert invoice is not None, method
    return Invoice.model_validate(invoice), method


def complete(invoice):
    return engines._basic_checks(invoice, engines._open_gaps(invoice, []))


@pytest.mark.parametrize("source", [pdf_read("plain-invoice.pdf"), scan_read("plain-invoice-scan")],
                         ids=["pdf", "scan"])
def test_a_plain_invoice_without_a_template_is_read_whole_by_the_local_parsers(source):
    invoice, _ = read(source)
    assert (invoice.number, str(invoice.date), invoice.supplier_name, invoice.buyer_name) == (
        "HLS-24017", "2026-10-24", "Harbour Lane Stationers LLC", "Retail UAE")
    assert (invoice.currency, invoice.net, invoice.tax) == ("AED", Decimal("621.00"), Decimal("31.05"))
    assert [(line.sku, line.qty, line.uom, line.price) for line in invoice.lines] == [
        ("HLS-1001", 20, "PCS", Decimal("18.50")), ("HLS-2040", 50, "BOX", Decimal("2.40")),
        ("HLS-3310", 4, "PCS", Decimal("32.75"))]
    assert complete(invoice) and engines.quality(invoice) == (1.0, [])


def test_a_footer_sentence_is_not_read_as_the_supplier():
    # "Not a real supplier invoice." holds the word "supplier" mid-sentence; it labels no party.
    invoice, _ = read(pdf_read("plain-invoice.pdf"))
    assert invoice.supplier_name == "Harbour Lane Stationers LLC"


@pytest.mark.parametrize("sample", ["plain-invoice.pdf", "plain-invoice-scan.png"])
def test_a_plain_invoice_makes_no_ai_call(monkeypatch, tmp_path, sample):
    (tmp_path / "work").mkdir()
    monkeypatch.delenv("INV_STUDIO_AI_CROSS_CHECK", raising=False)
    monkeypatch.setattr(engines, "capabilities", lambda: [
        {"id": e, "installed": True, "models_blocked": False, "version": "1", "note": ""}
        for e in ("invoice2data", "paddleocr", "docling")])

    def local_read(engine, path, root, language="en", extra=()):
        # A scan has no text layer; PaddleOCR's recorded read stands in for the OCR reader.
        if path.suffix == ".png":
            if engine == "invoice2data":
                return {"text": "", "invoice": None, "boxes": [], "tables": []}
            source = scan_read(path.stem)
        else:
            source = pdf_read(path.name)
        invoice, _ = structured_extract(source[0], source[1], [], source[2])
        return {"text": source[0], "invoice": invoice, "boxes": source[1], "tables": source[2]}

    calls = []

    def ai_reader(path, text, options):
        calls.append(path)
        raise AssertionError("the AI must not be asked")

    monkeypatch.setattr(engines, "local_read", local_read)
    result = engines.process(SAMPLES / sample, ProcessingOptions(engine="auto", model="m"),
                             types.SimpleNamespace(root=tmp_path), ai_reader)
    assert calls == [] and result["readers"]["ai"]["calls"] == 0
    assert result["invoice"]["number"] == "HLS-24017" and result["invoice"]["net"] == "621.00"


@pytest.mark.parametrize("source", [pdf_read("invoice.pdf"), scan_read("invoice-scan")], ids=["pdf", "scan"])
def test_the_demo_layout_reads_without_its_template(source):
    invoice, method = read(source)
    assert method == "layout"
    assert (invoice.number, invoice.supplier_name, invoice.buyer_name) == (
        "DEMO-2026-001", "Lumena Beauty Trading LLC", "Retail UAE")
    assert invoice.net == Decimal("800.00") and invoice.tax == Decimal("40.00")
    assert [(line.sku, line.gtin, line.qty, line.uom, line.price) for line in invoice.lines] == [
        ("LUM-SER30", "00012345678905", 10, "EA", Decimal("60.00")),
        ("LUM-CRM50", "00012345678912", 5, "EA", Decimal("40.00"))]
    assert complete(invoice)


def test_a_stray_mark_before_the_net_stands_only_when_the_lines_add_up_to_it():
    text, boxes, _ = scan_read("invoice-scan")
    assert "Net total: 8 800.00" in text
    # Lines that do not add up to the last amount: the net and the headerless lines are both left unread.
    altered = text.replace("10 EA 60.00", "11 EA 60.00")
    invoice, _ = read((altered, [], []))
    assert invoice.net is None and invoice.lines == []


def test_the_demo_template_reads_the_net_past_a_stray_mark_on_a_scan():
    # C2 of the diagnosis: PaddleOCR reads "Net total: 8 800.00" on the demo scan; the net is 800.00, not 8.0.
    text, _, _ = scan_read("invoice-scan")
    templates = templates_from([engines.ROOT / "templates"])
    for printed in ("Net total: 8 800.00", "Net total: 800.00"):
        invoice = Invoice.model_validate(template_extract(text.replace("Net total: 8 800.00", printed), templates))
        assert invoice.net == Decimal("800.00") and invoice.tax == Decimal("40.00"), printed


@pytest.mark.parametrize("cited", ["Reference invoice: INV-5511",
                                   "Payment is due 30 days from the date of this invoice: 24/11/2026"])
def test_a_line_that_cites_an_invoice_does_not_cost_a_text_read_its_number(cited):
    # A .txt, .csv or .docx reaches the layout parser with no word boxes, so the number comes from the text alone.
    from app.layout_extract import extract_invoice
    text = f"TAX INVOICE\nInvoice No: INV-5512\n{cited}\nDate: 2026-10-24\n"
    assert extract_invoice(text, None)["number"] == "INV-5512"


@pytest.mark.parametrize("keyword", ["", "Lumena Beauty Trading LLC\n"], ids=["layout", "demo-template"])
@pytest.mark.parametrize("line", [lambda n: f"Net total: {'7' * n}x", lambda n: f"Net total{' ' * n}x",
                                  lambda n: "Net total:" * (n // 10), lambda n: f"Invoice date{' ' * n}x"],
                         ids=["digits", "spaces", "labels", "date-spaces"])
def test_a_long_line_is_read_in_linear_time(keyword, line):
    # The stored text is capped at 150,000 characters, and a later export parses it in the API process.
    # A line ten times longer takes at most ten times as long; a backtracking regex takes about a hundred,
    # and seconds already at 15,000 characters. The ratio of the best of five runs holds on a loaded host,
    # where a wall-clock bound does not.
    import time
    templates = templates_from([engines.ROOT / "templates"])

    def seconds(n):
        text = f"{keyword}Invoice: DEMO-1\n{line(n)}\n"
        runs = []
        for _ in range(5):
            started = time.perf_counter()
            structured_extract(text, [], templates, [])
            runs.append(time.perf_counter() - started)
        return min(runs)
    short, long = seconds(1_500), seconds(15_000)
    assert long < 25 * short, f"{long:.3f}s for 15,000 characters, {short:.3f}s for 1,500"


@pytest.mark.parametrize("printed, field, value", [
    ("Invoice # FRE-1009", "number", "FRE-1009"),  # '#' then a space: still an invoice title
    ("Tax Invoice\nDoc No: PTX-20017", "number", "PTX-20017"),
    ("Tax Invoice SX/2026/0101", "number", "SX/2026/0101"),
    ("Customer PO 70801", "po", "70801"),
    ("Order Ref 70522", "po", "70522"),
    ("LPO Ref LPO-77310", "po", "77310"),  # docs/FIELD_RULES.md: the LPO word is not part of po
    ("PO Number: 14000001 TESTER", "po", "14000001"),
])
def test_header_labels_without_a_colon_are_read(printed, field, value):
    from app.layout_extract import extract_invoice
    text = f"Sample Trading LLC\nINVOICE\n{printed}\nDate: 2026-10-24\nInvoice No: INV-1\n" if field == "po" \
        else f"Sample Trading LLC\n{printed}\nDate: 2026-10-24\n"
    assert extract_invoice(text, None)[field] == value


def test_a_right_aligned_number_keeps_its_printed_space():
    from app.layout_extract import extract_invoice

    def word(text, x0, x1, y):
        return {"text": text, "page": 1, "box": [x0, y, x1, y + 10], "size": [595, 842]}
    boxes = [word("Sample", 42, 90, 40), word("LLC", 94, 120, 40), word("INVOICE", 495, 553, 40),
             word("Invoice", 349, 378, 70), word("Number", 380, 412, 70), word("NWT", 494, 514, 70),
             word("004512", 517, 547, 70), word("Date:", 349, 375, 90), word("2026-10-24", 380, 430, 90)]
    text = "Sample LLC INVOICE\nInvoice Number NWT 004512\nDate: 2026-10-24\n"
    assert extract_invoice(text, boxes)["number"] == "NWT 004512"


def test_a_po_printed_under_its_header_table_heading_is_read():
    from app.layout_extract import extract_invoice

    def word(text, x0, x1, y):
        return {"text": text, "page": 1, "box": [x0, y, x1, y + 10], "size": [595, 842]}
    boxes = [word("TAX", 250, 275, 30), word("INVOICE", 279, 330, 30),
             word("Doc", 40, 60, 80), word("No", 63, 76, 80), word("Purchase", 200, 245, 80),
             word("Order", 248, 275, 80), word("Salesman", 360, 410, 80),
             word("PTX-20017", 40, 95, 96), word("PO-70412", 205, 255, 96), word("S12", 365, 385, 96)]
    text = "TAX INVOICE\nDoc No Purchase Order Salesman\nPTX-20017 PO-70412 S12\n"
    assert extract_invoice(text, boxes)["po"] == "70412"
