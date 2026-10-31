"""Extraction regression tests for the local reader chain and the AI merge rules.

Real supplier invoices are customer data and are never committed here. Two fixture
layouts stand in for the ones in production: a columnar tax invoice whose totals row
is printed without a label, and a label/value header layout with an item-reference and
barcode column. Point INV_OCR_EVAL_DIR at a local directory of real invoices and
INV_OCR_EVAL_TRUTH at its proved ground-truth JSON to also run the per-field accuracy
thresholds over the real set. That set may hold images as well as PDFs: a scan or a
photograph is read by the OCR engine, with no text layer to try first.
"""

import json
import os
import re
from datetime import date, timedelta
from itertools import groupby
from decimal import Decimal
from pathlib import Path

import pytest

from app import vertex
from app.docling_extract import (
    _header_roles,
    _resolve_printed_amount_column,
    _semantic_row_roles,
    _take_printed_totals_row,
    extract_invoice_from_tables,
)
from app.engines import (
    _open_gaps,
    guard_amounts,
    guard_header,
    merge_ai_fields,
    quality,
    rounding_tolerance,
)
from app.layout_extract import (
    _extract_currency,
    _extract_spatial_totals,
    _group_page_rows,
    _header_anchors,
    _normalise_words,
    _page_slope,
    _parse_date,
    _row_numbers,
    extract_invoice,
    reading_rows,
    row_text,
)
from app.models import Invoice, parse_ai_output
from app.ocr_worker import (
    _trim_split_lines,
    _unreadable_hint,
    digital,
    spatial_text,
    structured_extract,
    templates_from,
)
from app.vertex import INCOMPLETE, TIMED_OUT, VertexProvider


TEMPLATES = templates_from([Path(__file__).resolve().parents[1] / "app/templates"])


def _text(line):
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph

    return Paragraph(line, getSampleStyleSheet()["Normal"])


def _page_break():
    from reportlab.platypus import PageBreak

    return PageBreak()


def _grid(data, widths=None):
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    table = Table(data, colWidths=widths)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def _write(path, flowables):
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate

    SimpleDocTemplate(str(path), pagesize=A4, topMargin=24, bottomMargin=24,
                      leftMargin=18, rightMargin=18).build(list(flowables))
    return path


def _paragraph(text):
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph

    return Paragraph(text, getSampleStyleSheet()["BodyText"])


COLUMNAR_LINES = [
    ("1", "900-9001-000001", "105550 WIDGET BLUE 50ML", "3", "100.00", "300.00", "15.00", "315.00"),
    ("2", "900-9001-000002", "105580 WIDGET RED 30ML", "2", "50.50", "101.00", "5.05", "106.05"),
    ("3", "900-9001-000003", "105680 WIDGET GREEN 90ML", "10", "12.25", "122.50", "6.13", "128.63"),
]


@pytest.fixture(scope="module")
def columnar_invoice(tmp_path_factory):
    """A tax invoice whose totals row is bare numbers and whose date year has two digits."""
    header = _grid([
        ["Customer Name", "ACME Retail Co. LLC", "Invoice Number", "INV-TEST-001"],
        ["Customer Number", "90001", "Invoice Date", "14-AUG-26"],
        ["TRN:100000000000003", "", "Order Number", "900002"],
        ["Customer Address", "P.O.Box: 1277, Dubai", "LPO Number", "70000002"],
    ], widths=[90, 170, 90, 130])
    rows = [["S.No", "Item", "Item Description", "Qty", "Price", "Disc", "Total",
             "Vat\n%", "Vat\nAmount", "Total Incl.Vat"]]
    rows += [[no, item, name, qty, price, "", total, "5", vat, incl]
             for no, item, name, qty, price, total, vat, incl in COLUMNAR_LINES]
    rows += [["", "", "", "15", "", "", "523.50", "", "26.18", "549.68"]]
    return _write(tmp_path_factory.mktemp("columnar") / "invoice.pdf", [
        _paragraph("TAX INVOICE"), header, _grid(rows), _paragraph("Amount in words:"),
        _paragraph("Five hundred forty-nine AED and 68 Fils"),
    ])


@pytest.fixture(scope="module")
def reference_invoice(tmp_path_factory):
    """A label/value header layout with an item-reference column and printed barcodes."""
    header = _grid([
        ["Invoice No.", "Invoice Date", "Currency", "LPO No."],
        ["190000000001", "01-Oct-2026", "KWD", "70000001"],
    ], widths=[120, 120, 90, 120])
    rows = [
        ["Sr.", "Item Ref", "Description", "Bar Code", "Qty", "UOM", "Price", "Disc%", "Amount"],
        ["1", "009900000001", "WIDGET VELVET 63", "4006381333931", "5", "Each", "21.500", "0.000", "107.500"],
        ["2", "009900000002", "WIDGET CRUSH 50ML", "5901234123457", "24", "Each", "52.000", "0.000", "1,248.000"],
    ]
    return _write(tmp_path_factory.mktemp("reference") / "invoice.pdf", [
        _paragraph("Sales Invoice"), header, _grid(rows),
        _grid([["Total Quantity", "29", "Net Amount", "1,355.500"]], widths=[100, 60, 100, 100]),
    ])


class _NeedsOcr(Exception):
    """The page carries no text layer, and the OCR engine that reads one is not installed."""


# Photographs and scans arrive as images, which carry no text layer to try first.
_IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def _read(path):
    if Path(path).suffix.lower() in _IMAGES:
        text, boxes, tables = "", [], []
    else:
        text, boxes, tables = digital(path, with_tables=True)
    if len(text.split()) < 20:
        # A photographed page: the service reads it with the OCR engine, not its text layer.
        try:
            from app.ocr_worker import paddle

            text, boxes = paddle(Path(path), "en")
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise _NeedsOcr(str(error)) from error
        tables = []
    invoice, method = structured_extract(text, boxes, TEMPLATES, tables)
    assert invoice is not None, f"no structured invoice ({method})"
    return invoice, method


def test_columnar_invoice_header_is_read(columnar_invoice):
    invoice, _method = _read(columnar_invoice)
    assert invoice["number"] == "INV-TEST-001"
    assert invoice["date"] == "2026-08-14"  # two-digit year, alphabetic month
    assert invoice["po"] == "70000002"
    assert invoice["currency"] == "AED"  # read from the amount in words
    assert Decimal(invoice["net"]) == Decimal("523.50")  # unlabelled totals row
    assert Decimal(invoice["tax"]) == Decimal("26.18")


def test_columnar_invoice_lines_are_read(columnar_invoice):
    invoice, _method = _read(columnar_invoice)
    assert len(invoice["lines"]) == len(COLUMNAR_LINES)
    for line, expected in zip(invoice["lines"], COLUMNAR_LINES):
        no, item, name, qty, price, total, vat, _incl = expected
        assert line["sku"] == item  # the column headed "Item"
        assert name.split(" ", 1)[1] in line["description"]
        assert Decimal(line["qty"]) == Decimal(qty)
        assert Decimal(line["price"]) == Decimal(price)
        # the bare "Total" column, proved by quantity x price, not the tax-inclusive one
        assert Decimal(line["net_amount"]) == Decimal(total)
        assert Decimal(line["tax_amount"]) == Decimal(vat)


def test_columnar_invoice_reconciles(columnar_invoice):
    invoice, _method = _read(columnar_invoice)
    assert sum(Decimal(x["net_amount"]) for x in invoice["lines"]) == Decimal(invoice["net"])
    assert sum(Decimal(x["tax_amount"]) for x in invoice["lines"]) == Decimal(invoice["tax"])


def test_reference_invoice_is_read(reference_invoice):
    invoice, _method = _read(reference_invoice)
    assert invoice["number"] == "190000000001"
    assert invoice["date"] == "2026-10-01"
    assert invoice["currency"] == "KWD"
    assert invoice["po"] == "70000001"
    assert Decimal(invoice["net"]) == Decimal("1355.500")
    assert [line["sku"] for line in invoice["lines"]] == ["009900000001", "009900000002"]
    assert [line["gtin"] for line in invoice["lines"]] == ["4006381333931", "5901234123457"]
    assert sum(Decimal(x["net_amount"]) for x in invoice["lines"]) == Decimal(invoice["net"])


@pytest.mark.parametrize("printed,expected", [
    ("14-AUG-26", "2026-08-14"),
    ("14-AUG-2026", "2026-08-14"),
    ("AUG-14-2026", "2026-08-14"),
    ("14 August 2026", "2026-08-14"),
    ("2026-08-14", "2026-08-14"),
    ("14/08/2026", "2026-08-14"),
    ("07 / 30 / 2026", "2026-07-30"),   # a page reader returns each separator as its own word
    ("05/06/2026", None),      # all-numeric and ambiguous: never guessed
    ("14-AUG-99", None),       # outside the invoicing window, so not guessed as 1999 or 2099
    ("31-FEB-26", None),
    ("14-XYZ-26", None),
])
def test_printed_dates(printed, expected):
    assert _parse_date(printed) == expected


@pytest.mark.parametrize("line,expected", [
    ("Five hundred forty-nine AED and 68 Fils", "AED"),
    ("Four hundred fifty AED Only", "AED"),
    ("Two thousand KWD and 500 Fils", "KWD"),
    ("Total in USD 12.00", "USD"),
    ("Total Amount Due (Incl. VAT)  AED  21,604.80", "AED"),
    ("no currency printed here", None),
])
def test_currency_in_words(line, expected):
    assert _extract_currency([line]) == expected


def test_amount_column_is_taken_only_when_quantity_times_price_proves_it():
    lines = [
        {"qty": "3", "price": "100.00", "_amount_columns": {6: "300.00", 9: "315.00"}},
        {"qty": "2", "price": "50.50", "_amount_columns": {6: "101.00", 9: "106.05"}},
    ]
    _resolve_printed_amount_column(lines, columns := set())
    assert [line["net_amount"] for line in lines] == ["300.00", "101.00"]
    assert "net_amount" in columns


def test_ambiguous_amount_columns_are_left_empty():
    lines = [{"qty": "3", "price": "100.00", "_amount_columns": {6: "290.00", 9: "315.00"}}]
    _resolve_printed_amount_column(lines, set())
    assert lines[0].get("net_amount") is None


def test_unlabelled_totals_row_is_taken_only_when_it_equals_the_lines():
    invoice = {"lines": [{"net_amount": "300.00", "tax_amount": "15.00"},
                         {"net_amount": "101.00", "tax_amount": "5.05"}]}
    _take_printed_totals_row(invoice, [("pdfplumber", {6: "401.00", 8: "20.05"})],
                             {"pdfplumber": {6: "net_amount", 8: "tax_amount"}})
    assert invoice["net"] == "401.00" and invoice["tax"] == "20.05"


def test_unlabelled_totals_row_is_refused_when_it_does_not_equal_the_lines():
    invoice = {"lines": [{"net_amount": "300.00", "tax_amount": "15.00"}]}
    _take_printed_totals_row(invoice, [("pdfplumber", {6: "900.00", 8: "45.00"})],
                             {"pdfplumber": {6: "net_amount", 8: "tax_amount"}})
    assert "net" not in invoice and "tax" not in invoice


def test_ai_lines_stand_in_when_they_sum_to_the_printed_total():
    engine = {"number": "X", "net": None, "lines": [{"net_amount": "5", "qty": "1", "price": "5", "description": "a"}]}
    ai = {"number": "X", "net": "30.00", "lines": [
        {"net_amount": "10", "qty": "1", "price": "10", "description": "a"},
        {"net_amount": "20", "qty": "1", "price": "20", "description": "b"}]}
    merged, _evidence, header_readers, _line_readers, notes = merge_ai_fields(
        engine, ai, {}, {}, "vertex", "fallback")
    assert len(merged["lines"]) == 2
    assert header_readers["net"] == "ai"
    assert any("sum to the printed total" in note for note in notes)


def test_ai_lines_are_refused_when_they_do_not_sum_to_the_printed_total():
    engine = {"number": "X", "net": None, "lines": [{"net_amount": "5", "qty": "1", "price": "5", "description": "a"}]}
    ai = {"number": "X", "net": "31.00", "lines": [
        {"net_amount": "10", "qty": "1", "price": "10", "description": "a"},
        {"net_amount": "20", "qty": "1", "price": "20", "description": "b"}]}
    merged, _evidence, _header_readers, _line_readers, _notes = merge_ai_fields(
        engine, ai, {}, {}, "vertex", "fallback")
    assert len(merged["lines"]) == 1


def test_ai_values_keep_printed_text_on_one_line():
    invoice, _evidence = parse_ai_output(json.dumps({
        "number": "X1", "buyer_name": "ACME Retail Co.\nLLC",
        "lines": [{"description": "WIDGET  BLUE\n50ML", "qty": "1"}]}))
    assert invoice.buyer_name == "ACME Retail Co. LLC"
    assert invoice.lines[0].description == "WIDGET BLUE 50ML"


# --- Accuracy thresholds over a local set of real invoices (opt-in) ---------------

THRESHOLDS = {
    "number": 0.98, "date": 0.98, "currency": 0.98, "net": 0.98, "tax": 0.98, "po": 0.98,
    "lines": 0.98, "sku": 0.98, "qty": 0.98, "price": 0.98,
    "net_amount": 0.98, "tax_amount": 0.98, "description": 0.95,
}


def _same(field, left, right):
    if field in ("net", "tax", "qty", "price", "net_amount", "tax_amount"):
        try:
            return Decimal(str(left).replace(",", "")) == Decimal(str(right).replace(",", ""))
        except Exception:
            return False
    # Some printed values cannot be resolved from the page alone - "05.04.2025" is either
    # the 5th of April or the 4th of May - and the right answer is to claim nothing
    # and send the field back for review. Ground truth says so with "review", and the read
    # counts as right only when it leaves the field empty.
    if str(left).strip().casefold() == "review":
        return right in (None, "")
    if left in (None, "") or right in (None, ""):
        return False
    clean = lambda value: " ".join(re.sub(r"[^0-9a-z ]", " ", str(value).casefold()).split())
    return clean(left) == clean(right)


def _words(rows, *, page=1, size=(1222, 1668), height=15):
    """Word boxes as a page reader hands them over: one dict per word, with its own box."""
    boxes = []
    for y, line in rows:
        for text, x0, x1 in line:
            boxes.append({"text": text, "page": page, "box": [x0, y, x1, y + height], "size": list(size)})
    return boxes


def _tilt(boxes, slope, anchor=600.0):
    """The same words as a page scanned a little off square: y rises with x."""
    out = []
    for box in boxes:
        x0, y0, x1, y1 = box["box"]
        shift = (x0 - anchor) * slope
        out.append({**box, "box": [x0, y0 + shift, x1, y1 + shift]})
    return out


# Columns across the width of a page, as a real invoice table prints them.
TABLE_COLUMNS = [102, 200, 300, 450, 550, 650, 735, 790, 850, 900, 970, 1030, 1100]


def _table_rows(count, pitch=15.5):
    """A table of plain words, one word under each column heading."""
    rows = [(600, [(f"HEAD{index}", x, x + 40) for index, x in enumerate(TABLE_COLUMNS)])]
    for row in range(count):
        rows.append((630 + pitch * row,
                     [(f"w{row}-{index}", x, x + 40) for index, x in enumerate(TABLE_COLUMNS)]))
    return _words(rows)


def test_a_tilted_scan_is_squared_before_rows_are_grouped():
    """Three-quarters of a degree of tilt puts a row's text and its figures in different rows."""
    rows = _group_page_rows(_normalise_words(_tilt(_table_rows(13), 0.0131)))
    assert len(rows) == 14, [[word.text for word in row] for row in rows]
    assert all(len(row) == len(TABLE_COLUMNS) for row in rows), [
        [word.text for word in row] for row in rows
    ]


def test_a_square_page_is_left_alone():
    assert _page_slope(_normalise_words(_table_rows(13))) == 0.0


def test_two_label_columns_printed_close_together_stay_apart():
    """The invoice number and the PO number are stacked lines, not one row."""
    boxes = _words([
        (462, [("INVOICE", 828, 858), ("#", 863, 865), (":", 872, 874), ("14004297", 881, 912)]),
        (470, [("PO", 832, 838), ("Number", 844, 872), (":", 872, 874), ("300726", 919, 941)]),
    ])
    rows = _group_page_rows(_normalise_words(boxes))
    texts = [" ".join(word.text for word in row) for row in rows]
    assert len(rows) == 2, texts
    assert any("14004297" in text and "300726" not in text for text in texts), texts


def test_a_thousands_separator_read_as_its_own_word_is_one_number():
    row = _group_page_rows(_normalise_words(_words([
        (900, [("VAT", 700, 730), ("AMOUNT", 735, 800), ("1", 898, 901), (",", 903, 906), ("028.80", 908, 933)]),
    ])))[0]
    assert _row_numbers(row) == [Decimal("1028.80")]


def test_printed_totals_are_read_from_their_own_labels():
    """"Total Amount (exl. VAT)" is the net; the same words with "Incl." are not."""
    words = _normalise_words(_words([
        (894, [("Total", 700, 730), ("Amount", 735, 790), ("(", 792, 796), ("exl", 798, 812),
               (".", 813, 815), ("VAT", 817, 840), (")", 842, 845),
               ("20", 1000, 1012), (",", 1014, 1017), ("576.00", 1019, 1050)]),
        (908, [("VAT", 700, 730), ("AMOUNT", 735, 800), ("@", 805, 812), ("5", 815, 820),
               ("%", 822, 828), ("1", 1004, 1008), (",", 1010, 1013), ("028.80", 1015, 1050)]),
        (921, [("Total", 700, 730), ("Amount", 735, 790), ("Due", 795, 820), ("(", 822, 826),
               ("Incl", 828, 845), (".", 846, 848), ("VAT", 850, 873), (")", 875, 878),
               ("21", 1000, 1012), (",", 1014, 1017), ("604.80", 1019, 1050)]),
    ]))
    assert _extract_spatial_totals(words) == (Decimal("20576.00"), Decimal("1028.80"))


def test_a_tax_total_label_read_as_one_word_is_still_a_tax_total():
    """A scan's reader returns "VATAMOUNT @5%" with no space in the label."""
    words = _normalise_words(_words([
        (894, [("Total", 700, 730), ("Amount", 735, 790), ("(", 792, 796), ("exl", 798, 812),
               (".", 813, 815), ("VAT", 817, 840), (")", 842, 845),
               ("20", 1000, 1012), (",", 1014, 1017), ("576.00", 1019, 1050)]),
        (908, [("VATAMOUNT", 700, 800), ("@", 805, 812), ("5", 815, 820), ("%", 822, 828),
               ("1", 1004, 1008), (",", 1010, 1013), ("028.80", 1015, 1050)]),
    ]))
    assert _extract_spatial_totals(words) == (Decimal("20576.00"), Decimal("1028.80"))


def test_a_bare_unit_opens_a_price_label_and_is_not_a_unit_of_measure():
    """OCR puts the bracketed note between "Unit" and "Price"; a UOM right of Qty would
    otherwise make the whole table unreadable."""
    row = _group_page_rows(_normalise_words(_words([
        (600, [("DESCRIPTION", 505, 554), ("QUANTITY", 724, 762), ("(", 778, 780),
               ("UNIT", 776, 793), ("exlL", 781, 795), ("VAT", 797, 813), ("PRICE", 797, 816),
               (")", 813, 816), ("MARGIN", 838, 870)]),
    ])))[0]
    anchors = _header_anchors(row)
    assert "uom" not in anchors
    assert {"description", "qty", "price"} <= set(anchors)


@pytest.mark.skipif(not os.environ.get("INV_OCR_EVAL_DIR"), reason="no local invoice set configured")
def test_real_invoice_set_meets_the_field_thresholds():
    directory = Path(os.environ["INV_OCR_EVAL_DIR"])
    truth_file = Path(os.environ.get("INV_OCR_EVAL_TRUTH") or directory / "truth.json")
    truth = {record["file"]: record for record in json.loads(truth_file.read_text())
             if record.get("proved")}
    assert truth, f"no proved ground truth in {truth_file}"
    hit = {field: 0 for field in THRESHOLDS}
    total = {field: 0 for field in THRESHOLDS}
    unread = []
    for name, expected in sorted(truth.items()):
        try:
            invoice, _method = _read(directory / name)
        except _NeedsOcr:
            unread.append(name)
            continue
        for field in ("number", "date", "currency", "net", "tax", "po"):
            total[field] += 1
            hit[field] += bool(_same(field, expected.get(field), invoice.get(field)))
        total["lines"] += 1
        read_lines = invoice.get("lines") or []
        if len(read_lines) != len(expected["lines"]):
            for field in ("sku", "qty", "price", "net_amount", "tax_amount", "description"):
                total[field] += len(expected["lines"])
            continue
        hit["lines"] += 1
        for want, got in zip(expected["lines"], read_lines):
            for field in ("sku", "qty", "price", "net_amount", "tax_amount", "description"):
                total[field] += 1
                hit[field] += bool(_same(field, want.get(field), got.get(field)))
    assert len(unread) < len(truth), f"the OCR engine read none of the set: {unread}"
    measured = {field: hit[field] / total[field] for field in THRESHOLDS if total[field]}
    short = {field: round(value, 4) for field, value in measured.items()
             if value < THRESHOLDS[field]}
    assert not short, f"below threshold: {short} (measured {measured})"


@pytest.mark.parametrize(
    "printed,kept",
    [
        (date.today().isoformat(), True),
        ((date.today() + timedelta(days=30)).isoformat(), True),
        ((date.today() - timedelta(days=9 * 365)).isoformat(), True),
        ((date.today() + timedelta(days=800)).isoformat(), False),
        (f"{date.today().year - 11}-08-14", False),
        ("1926-08-14", False),
    ],
)
def test_a_date_outside_the_invoicing_window_goes_back_for_review(printed, kept):
    """A plausible date stands; an implausible one is cleared and flagged, whichever reader read it."""
    invoice = {"date": printed, "tax": "1.00", "lines": []}
    readers = {"date": "ai"}
    result, evidence = guard_header(invoice, {"header": {}}, readers, "ai", "")
    if kept:
        assert result["date"] == printed
        assert readers["date"] == "ai"
    else:
        assert result["date"] is None
        assert "date" not in readers
        review = evidence["header"]["date"]["review"]
        assert review["other_value"] == printed
        assert "needs checking" in review["reason"]


# A cut-off AI read: the whole document is asked for twice, then taken apart page by page.

class _CutOff(VertexProvider):
    """A provider that cannot return the whole document but answers a single page fine."""

    def __init__(self, whole_error=INCOMPLETE):
        self.whole_error = whole_error
        self.calls = []

    def _generate(self, project, location, model, prompt, parts):
        whole = any("Document text" in part.get("text", "") for part in parts)
        self.calls.append("whole" if whole else "page")
        if whole:
            raise ValueError(self.whole_error)
        read = self.calls.count("page")
        invoice = Invoice.model_validate({
            "number": "190000000001",
            "net": "10.00",
            "lines": [{"description": f"item {read}", "net_amount": "10.00", "page": 1}],
        })
        return invoice, {"candidatesTokenCount": 7}


@pytest.fixture
def two_page_pdf(tmp_path):
    return _write(tmp_path / "two-page.pdf", [
        _text("Tax Invoice 190000000001"),
        _page_break(),
        _text("Continued"),
    ])


@pytest.fixture
def _vertex_configured(monkeypatch):
    monkeypatch.setattr(vertex, "_configuration", lambda model: ("project", "global", model))


def test_a_cut_off_read_is_asked_for_again_before_the_document_is_split(two_page_pdf, _vertex_configured):
    """A read that comes back cut off is often whole on the next attempt, so it is retried once."""
    provider = _CutOff()
    invoice, meta = provider.extract(two_page_pdf, "", "gemini-3.7-flash", "prompt")
    assert provider.calls == ["whole", "whole", "page", "page"]
    assert meta["chunks"] == 2
    assert [line.page for line in invoice.lines] == [1, 2]
    assert [line.description for line in invoice.lines] == ["item 1", "item 2"]


def test_a_two_page_invoice_is_split_a_page_at_a_time(two_page_pdf, _vertex_configured):
    """A short document is halved rather than left whole, which is what the page limit alone did."""
    provider = _CutOff(whole_error=TIMED_OUT)
    invoice, meta = provider.extract(two_page_pdf, "", "gemini-3.7-flash", "prompt")
    # A timeout is not retried whole: the same request would time out again.
    assert provider.calls == ["whole", "page", "page"]
    assert meta["chunks"] == 2
    assert invoice.number == "190000000001"


def test_a_cut_off_single_page_read_is_reported_rather_than_split(tmp_path, _vertex_configured):
    """There is nothing to split, so the retry is the whole answer and the reason is passed on."""
    path = _write(tmp_path / "one-page.pdf", [_text("Tax Invoice 190000000001")])
    provider = _CutOff()
    with pytest.raises(ValueError, match="complete"):
        provider.extract(path, "", "gemini-3.7-flash", "prompt")
    assert provider.calls == ["whole", "whole"]


# A column of amounts read one row out of place: every value is plausible and wrong.

def _shifted_invoice():
    """Twelve lines whose tax column was read one row down, as a scan's table mapper returned it."""
    lines, tax = [], None
    for number in range(1, 13):
        qty, price = Decimal(12), Decimal("10.00") + number
        lines.append({"no": number, "qty": str(qty), "price": str(price),
                      "tax_amount": None if tax is None else str(tax)})
        tax = (qty * price * Decimal("0.05")).quantize(Decimal("0.01"))
    net = sum(Decimal(line["qty"]) * Decimal(line["price"]) for line in lines)
    return {"net": str(net), "tax": str((net * Decimal("0.05")).quantize(Decimal("0.01"))), "lines": lines}


def test_a_tax_column_read_one_row_out_of_place_is_cleared():
    """The column does not sum to the printed tax, so each value is checked against its own row."""
    invoice = _shifted_invoice()
    result, evidence = guard_amounts(invoice, {}, [{} for _ in invoice["lines"]], "docling")
    assert [line["tax_amount"] for line in result["lines"]] == [None] * 12
    review = evidence["lines"][1]["tax_amount"]["review"]
    assert review["other_value"] == "6.60"
    assert "needs checking" in review["reason"]


def test_a_tax_column_that_sums_to_the_printed_total_is_left_alone():
    """The printed totals prove the column, so nothing is touched even where a row looks odd."""
    invoice = _shifted_invoice()
    lines = invoice["lines"]
    for line in lines:
        line["tax_amount"] = str((Decimal(line["qty"]) * Decimal(line["price"]) * Decimal("0.05")).quantize(Decimal("0.01")))
    invoice["tax"] = str(sum(Decimal(line["tax_amount"]) for line in lines))
    result, evidence = guard_amounts(invoice, {}, [{} for _ in lines], "docling")
    assert [line["tax_amount"] for line in result["lines"]] == [line["tax_amount"] for line in lines]
    assert all(not row for row in evidence.get("lines", []))


def test_lines_at_more_than_one_tax_rate_are_not_judged_by_the_printed_total():
    """A mixed-rate invoice implies no single rate, so the totals cannot contradict a line."""
    invoice = {"net": "200.00", "tax": "5.00", "lines": [
        {"qty": "1", "price": "100.00", "net_amount": "100.00", "tax_amount": "5.00"},
        {"qty": "1", "price": "100.00", "net_amount": "100.00", "tax_amount": "0.00"},
    ]}
    result, _ = guard_amounts(invoice, {}, [{}, {}], "paddleocr")
    assert [line["tax_amount"] for line in result["lines"]] == ["5.00", "0.00"]


def test_a_line_total_the_quantity_and_price_contradict_is_cleared():
    """The net column does not sum to the printed net, and this row's own figures disagree too."""
    invoice = {"net": "300.00", "tax": "15.00", "lines": [
        {"qty": "2", "price": "50.00", "net_amount": "100.00"},
        {"qty": "4", "price": "50.00", "net_amount": "150.00"},
    ]}
    result, evidence = guard_amounts(invoice, {}, [{}, {}], "docling")
    assert [line["net_amount"] for line in result["lines"]] == ["100.00", None]
    assert evidence["lines"][1]["net_amount"]["review"]["other_value"] == "150.00"


def test_a_total_amount_heading_says_which_column_is_the_line_net():
    """A scan's reader drops a letter from "excl."; the heading still says the column excludes VAT."""
    row = ["SN.", "BRAND", "BARCODE", "DESCRIPTION", "QUANTITY", "UNIT PRICE (exI. VAT)",
           "MARGIN", "Total Amount (exI. VAT)", "VAT", "VAT AMOUNT %s@", "AMOUNT (Incl. VAT)"]
    roles = _semantic_row_roles(row)
    assert roles[7] == "net_amount"
    assert roles[10] == "gross_amount"
    assert roles[9] == "tax_amount"


def _detections(rows, *, page=1, size=(1222, 1668), height=15):
    """Word boxes as an OCR reader hands them over: each word also names the printed text
    line it was cut from, so a value printed in parts keeps its spacing."""
    boxes = []
    for y, detections in rows:
        for detection in detections:
            printed = " ".join(text for text, _x0, _x1 in detection)
            for text, x0, x1 in detection:
                boxes.append({"text": text, "page": page, "box": [x0, y, x1, y + height],
                              "size": list(size), "parent_text": printed})
    return boxes


def _photographed_page():
    """A photographed tax invoice: amount headings the reader garbles, a barcode item
    column, an "INVOICE #" value printed apart from its label in three parts, party
    blocks printed side by side, and a totals row printed without a label."""
    return _detections([
        (100, [[("TAX", 550, 600), ("INVOICE", 605, 700)]]),
        (300, [[("Bill", 100, 130), ("To", 135, 150), (":", 152, 155)],
               [("From", 700, 730), (":", 732, 735)]]),
        (320, [[("MK", 100, 120), ("ZAHRA", 125, 170), ("INTERNATIONAL", 175, 280),
                ("CO", 285, 305), ("LLC", 310, 335)],
               [("AJA", 700, 730), ("International", 735, 820), ("General", 825, 885),
                ("Trading", 890, 945), ("LLC", 950, 975)]]),
        (462, [[("INVOICE", 700, 760), ("#", 763, 770), (":", 772, 775)],
               [("ULTA", 820, 858), ("TDM", 863, 898), ("300726", 903, 958)]]),
        (480, [[("PO", 700, 715), ("Number", 720, 770), (":", 772, 775)],
               [("14004297", 792, 860)]]),
        (498, [[("DATE", 700, 740), (":", 772, 775)], [("30-07-2026", 792, 880)]]),
        (600, [[("SN.", 102, 130)], [("BRAND", 200, 250)], [("BARCODE", 300, 370)],
               [("DESCRIPTION", 450, 560)], [("QUANTITY", 650, 715)],
               [("UNIT", 735, 765), ("PRICE", 770, 790)], [("MARGIN", 810, 865)],
               [("Totai", 880, 935)], [("VATAMUNT", 990, 1060)]]),
        (630, [[("1", 102, 110)], [("MEDICUBE", 200, 265)], [("8809653360403", 300, 400)],
               [("ZERO", 450, 490), ("PORE", 495, 535), ("PAD", 540, 570)],
               [("6", 650, 660)], [("65.47", 735, 785)], [("30%", 815, 845)],
               [("392.82", 880, 935)], [("19.64", 990, 1035)]]),
        (645, [[("2", 102, 110)], [("NUMBUZIN", 200, 265)], [("8809652589619", 300, 400)],
               [("NO", 450, 470), ("3", 475, 482), ("SKIN", 487, 525)],
               [("12", 650, 668)], [("23.10", 735, 785)], [("30%", 815, 845)],
               [("277.20", 880, 935)], [("13.86", 990, 1035)]]),
        (660, [[("3", 102, 110)], [("MEDICUBE", 200, 265)], [("8809653360410", 300, 400)],
               [("COLLAGEN", 450, 520), ("NIGHT", 525, 570)],
               [("4", 650, 660)], [("41.00", 735, 785)], [("30%", 815, 845)],
               [("164.00", 880, 935)], [("8.20", 990, 1030)]]),
        (690, [[("Total", 450, 490)], [("22", 650, 668)], [("834.02", 880, 935)],
               [("41.70", 990, 1035)]]),
        (894, [[("Total", 700, 730), ("Amount", 735, 790), ("(", 792, 796),
                ("exl", 798, 812), (".", 813, 815), ("VAT", 817, 840), (")", 842, 845)],
               [("834.02", 1019, 1070)]]),
        (908, [[("VAT", 700, 730), ("AMOUNT", 735, 800), ("@", 805, 812), ("5", 815, 820),
                ("%", 822, 828)], [("41.70", 1015, 1060)]]),
    ])


def _photographed_invoice():
    boxes = _photographed_page()
    text = "\n".join(row_text(row) for _page, row in reading_rows(boxes))
    invoice = extract_invoice(text, boxes)
    assert invoice is not None
    return invoice


def test_a_tilted_scan_reads_as_rows_and_not_as_columns():
    """The plain text a template parses follows the printed rows, so a column of
    figures is no longer handed over as one line of text."""
    lines = spatial_text(_tilt(_table_rows(3), 0.0131)).splitlines()
    assert len(lines) == 4, lines
    assert lines[1].split() == [f"w0-{index}" for index in range(len(TABLE_COLUMNS))], lines


def test_an_invoice_number_printed_in_spaced_parts_is_read_whole():
    """"INVOICE #" sits a word's width from its value, and the value is three parts."""
    invoice = _photographed_invoice()
    assert invoice["number"] == "ULTA TDM 300726"
    assert invoice["po"] == "14004297"


def test_unreadable_amount_headings_are_named_by_the_arithmetic():
    """"Total Amount" and "VAT AMOUNT" come back garbled; quantity times price still
    says which column is the line net, and the rate which is the tax."""
    lines = _photographed_invoice()["lines"]
    assert [line["net_amount"] for line in lines] == ["392.82", "277.20", "164.00"]
    assert [line["tax_amount"] for line in lines] == ["19.64", "13.86", "8.20"]


def test_a_barcode_column_is_the_item_reference():
    lines = _photographed_invoice()["lines"]
    assert [line["sku"] for line in lines] == [
        "8809653360403", "8809652589619", "8809653360410"]


def test_a_totals_row_printed_without_a_label_ends_the_table():
    """The row is the word "Total" and the figures it adds up. Unlabelled, it would
    otherwise read as more of the last line."""
    lines = _photographed_invoice()["lines"]
    assert len(lines) == 3, lines
    assert lines[-1]["description"] == "COLLAGEN NIGHT"
    assert "834.02" not in lines[-1]["evidence"], lines[-1]["evidence"]


def test_party_blocks_printed_side_by_side_are_read_apart():
    invoice = _photographed_invoice()
    assert invoice["buyer_name"] == "MK ZAHRA INTERNATIONAL CO LLC"
    assert invoice["supplier_name"] == "AJA International General Trading LLC"


@pytest.mark.parametrize("count, allowance", [(0, "0.01"), (1, "0.01"), (13, "0.13"), (400, "0.25")])
def test_the_printed_rounding_allowance_grows_a_minor_unit_a_line_and_then_stops(count, allowance):
    """Each line amount is rounded on its own, so a page of them may miss the printed
    total by a minor unit a line; the allowance is capped so it cannot hide a misread."""
    assert rounding_tolerance(count) == Decimal(allowance)


def _two_line_invoice(units):
    """A reconciled two-line invoice whose lines carry the units given, one per line."""
    lines = [{"sku": "A-1", "description": "ITEM ONE", "qty": "2", "price": "10.00",
              "net_amount": "20.00", "uom": units[0]},
             {"sku": "A-2", "description": "ITEM TWO", "qty": "1", "price": "5.00",
              "net_amount": "5.00", "uom": units[1]}]
    return Invoice.model_validate({"number": "INV-1", "date": "2026-07-30", "currency": "AED",
                                   "net": "25.00", "tax": "1.25", "lines": lines})


def test_a_page_that_prints_no_unit_of_measure_leaves_no_gap_to_fill():
    """Nothing on the page says PCS or BOX, so an empty unit is an absent field, not a short read.

    It is the difference between a complete invoice and one that sends a paid AI call after a
    field that was never printed.
    """
    invoice = _two_line_invoice([None, None])
    gaps = _open_gaps(invoice, quality(invoice)[1])
    assert not [gap for gap in gaps if gap.endswith(" uom")]
    assert not gaps


def test_a_unit_of_measure_printed_on_one_line_still_leaves_the_others_a_gap():
    """One line prints PCS, so the column exists and the line that lost its unit was read short."""
    invoice = _two_line_invoice(["PCS", None])
    gaps = _open_gaps(invoice, quality(invoice)[1])
    assert gaps == ["line 2 uom"]


def _region_boxes(boxes):
    """The same words as a reader that boxes a whole detected region hands them over:
    every word of a printed line carries that line's box, not its own."""
    out = []
    for _key, group in groupby(boxes, key=lambda box: box.get("parent_text") or id(box)):
        run = list(group)
        whole = [min(box["box"][0] for box in run), min(box["box"][1] for box in run),
                 max(box["box"][2] for box in run), max(box["box"][3] for box in run)]
        out.extend({**box, "box": list(whole)} for box in run)
    return out


def _cut_into_more_words(boxes, times):
    """The same page as a reader that cut each detected region into more words than it did,
    so a region's word count changes but nothing about where the page's lines sit."""
    return [dict(box) for box in boxes for _ in range(times)]


def _tilted_region_page(slope=0.0131):
    rows = [(120 + 24.0 * line,
             [[(f"l{line}-{word}", 90 + 46 * word, 130 + 46 * word) for word in range(10)],
              [(f"r{line}-{word}", 640 + 46 * word, 680 + 46 * word) for word in range(10)]])
            for line in range(9)]
    rows += [(600 + 15.5 * row, [[(f"w{row}-{column}", x, x + 40)]
                                 for column, x in enumerate(TABLE_COLUMNS)])
             for row in range(10)]
    return _region_boxes(_tilt(_detections(rows), slope))


def test_a_tilted_page_of_region_boxes_still_groups_into_its_printed_rows():
    """The same page, through the reader that groups rows: one row per printed row, whatever
    the word count of the lines above the table."""
    for times in (1, 4):
        boxes = _cut_into_more_words(_tilted_region_page(), times)
        rows = _group_page_rows(_normalise_words(boxes))
        table = [row for row in rows if any(word.text.startswith("w") for word in row)]
        assert len(table) == 10, [[word.text for word in row] for row in table]
        assert all(len(row) == len(TABLE_COLUMNS) * times for row in table), [
            len(row) for row in table]


def test_a_rate_heading_names_a_column_of_its_own():
    """"Disc %" and "VAT %" head rates, not amounts. A heading nothing recognises leaves
    its column unbounded, and its figures are read as part of the cell beside it, which
    then holds two numbers and parses as neither."""
    roles = _semantic_row_roles(
        ["SL NO", "ITEM CODE", "DESCRIPTION", "QTY", "PRICE", "Disc %",
         "Net Value", "VAT %", "VAT Amount", "Total Amount"])
    assert roles[5] == "discount"
    assert roles[7] == "tax_rate"
    assert roles[6] == "net_amount"
    assert roles[8] == "tax_amount"


def _heading_row(cells):
    return [{"text": text, "box": [x0, 600.0, x1, 615.0]} for text, x0, x1 in cells]


def test_a_rate_sign_beside_an_amount_heading_does_not_widen_it():
    """The "%" of "Disc %" is printed over the rate's figures, left of "Net Value". Read
    as part of that heading it widens the net column back over the rate, and the net cell
    comes back holding both figures."""
    row = _heading_row([("Disc", 650, 690), ("%", 729, 741), ("Net", 790, 822),
                        ("Value", 826, 874), ("VAT", 920, 952), ("%", 956, 968)])
    spans = {role: (left, right) for role, left, right in _header_roles(row)}
    assert spans["net_amount"][0] == 790.0, spans


def test_a_total_amount_heading_printed_as_two_boxes_names_one_column():
    """"Total" and "Amount" come back as separate boxes. Taken apart, "Total" names the
    gross column and "Amount" is left over to name a second net column, so a line's net
    is read from the wrong one."""
    row = _heading_row([("Net", 790, 822), ("Value", 826, 874), ("VAT", 920, 952),
                        ("Amount", 956, 1012), ("Total", 1060, 1105), ("Amount", 1109, 1165)])
    spans = _header_roles(row)
    roles = [role for role, _left, _right in spans]
    assert len(roles) == len(set(roles)), spans
    assert "net_amount" in roles and roles.count("net_amount") == 1, spans
    assert [(left, right) for role, left, right in spans if role != "net_amount"
            and left >= 1060] == [(1060.0, 1165.0)], spans


COLUMNAR_SCAN_COLUMNS = [
    ("serial", [("SL", 60, 82), ("NO", 86, 108)]),
    ("sku", [("ITEM", 140, 180), ("CODE", 184, 224)]),
    ("description", [("DESCRIPTION", 300, 410)]),
    ("qty", [("QTY", 520, 552)]),
    ("price", [("PRICE", 600, 648)]),
    ("discount", [("Disc", 700, 738), ("%", 742, 752)]),
    ("net", [("Net", 800, 832), ("Value", 836, 884)]),
    ("rate", [("VAT", 930, 962), ("%", 966, 976)]),
    ("tax", [("VAT", 1020, 1052), ("Amount", 1056, 1112)]),
    ("gross", [("Total", 1160, 1205), ("Amount", 1209, 1270)]),
]


def _scanned_columnar_page():
    """A columnar tax invoice as a scan's reader hands it over: a serial heading whose L
    reads as an I, rate columns beside their amounts, "Total Amount" in two boxes, and
    product names whose "+" is cut into words of its own."""
    def description(text):
        parts, x = [], 300
        for word in re.findall(r"\+|[^\s+]+", text):
            width = 6 * len(word)
            parts.append((word, x, x + width))
            x += width + 4
        return parts

    rows = [
        (300, [[("TAX", 560, 610), ("INVOICE", 614, 710)]]),
        (420, [[("Invoice", 700, 760), ("No", 764, 790), (":", 792, 795)],
               [("70001234", 820, 900)]]),
        (440, [[("Date", 700, 740), (":", 792, 795)], [("11.03.2024", 820, 910)]]),
        (600, [cells for _role, cells in COLUMNAR_SCAN_COLUMNS]),
        (650, [[("10", 64, 84)], [("AB12CD345E01", 140, 252)],
               description("ROSE EDP 50ML+CS50+CASE"), [("6", 536, 548)],
               [("500.00", 600, 650)], [("30.00", 705, 752)], [("2,100.00", 810, 884)],
               [("5.00", 935, 976)], [("105.00", 1060, 1112)], [("2,205.00", 1196, 1270)]]),
        (668, [[("20", 64, 84)], [("CD34EF567G02", 140, 252)],
               description("MIST EDT 75ML+SG75+10"), [("4", 536, 548)],
               [("400.00", 600, 650)], [("30.00", 705, 752)], [("1,120.00", 810, 884)],
               [("5.00", 935, 976)], [("56.00", 1070, 1112)], [("1,176.00", 1196, 1270)]]),
        (760, [[("Net", 700, 732), ("Amount", 736, 792), (":", 794, 797)],
               [("3,220.00", 820, 894)]]),
        (780, [[("VAT", 700, 732), ("Amount", 736, 792), (":", 794, 797)],
               [("161.00", 820, 894)]]),
    ]
    boxes = _detections(rows, size=(1300, 1750))
    # The reader reports the line as the page printed it, not respaced around the "+".
    printed = {" ".join(re.findall(r"\+|[^\s+]+", name)): name for name in
               ("ROSE EDP 50ML+CS50+CASE", "MIST EDT 75ML+SG75+10")}
    return [{**box, "parent_text": printed.get(box["parent_text"], box["parent_text"])}
            for box in boxes]


def _scanned_columnar_invoice():
    boxes = _scanned_columnar_page()
    text = "\n".join(row_text(row) for _page, row in reading_rows(boxes))
    invoice = extract_invoice_from_tables(text, None, boxes)
    assert invoice is not None
    return invoice


def test_a_scanned_columnar_invoice_reads_every_line_figure():
    """Each printed column is named or bounded, so no cell comes back holding two figures."""
    lines = _scanned_columnar_invoice()["lines"]
    assert [line["qty"] for line in lines] == ["6", "4"]
    assert [line["price"] for line in lines] == ["500.00", "400.00"]
    assert [line["net_amount"] for line in lines] == ["2100.00", "1120.00"]
    assert [line["tax_amount"] for line in lines] == ["105.00", "56.00"]


def test_a_serial_heading_read_as_sl_no_keeps_the_line_number_out_of_the_item_code():
    """"Sl No" is as common a serial heading as "S.No", and a scan reads its l as an I.
    Unrecognised, its column is unbounded and the line number joins the code beside it."""
    lines = _scanned_columnar_invoice()["lines"]
    assert [line["sku"] for line in lines] == ["AB12CD345E01", "CD34EF567G02"]


def test_a_description_keeps_the_spacing_the_page_printed():
    """A reader that reports the text line a word came from spaces that line as the page
    does, so a product name whose "+" is boxed on its own is not respaced around it."""
    lines = _scanned_columnar_invoice()["lines"]
    assert [line["description"] for line in lines] == [
        "ROSE EDP 50ML+CS50+CASE", "MIST EDT 75ML+SG75+10"]


@pytest.mark.parametrize(
    "name,content,says",
    [
        ("locked.pdf", b"%PDF-1.7\ntrailer<</Encrypt 9 0 R>>\n%%EOF", "password protected"),
        ("cut.pdf", b"%PDF-1.7\n" + b"stream " * 100, "incomplete"),
        ("empty.pdf", b"", "empty"),
        ("photo.jpg", b"\xff\xd8\xff\xe0 not a readable picture", "picture could not be read"),
        ("fine.pdf", b"%PDF-1.7\nobjects\n%%EOF", "model cache"),
    ],
)
def test_a_document_no_reader_can_open_says_why_in_words_the_uploader_can_act_on(
    tmp_path, name, content, says
):
    """A password-protected invoice told to check its model cache looks like a broken
    reader, and the one person who could fix it does nothing. A document that is sound,
    though, leaves the engine's own fault reported as the engine's."""
    path = tmp_path / name
    path.write_bytes(content)
    assert says in _unreadable_hint(path)


def test_an_unreadable_document_gives_nothing_of_the_document_away(tmp_path):
    path = tmp_path / "locked.pdf"
    path.write_bytes(b"%PDF-1.7\nBUYER LLC 70001234 1234.56\ntrailer<</Encrypt 9 0 R>>\n%%EOF")
    hint = _unreadable_hint(path)
    assert "BUYER" not in hint and "70001234" not in hint and "1234.56" not in hint


def _ocr_line(text, box, words):
    size = [1191, 1684]
    line = {"text": text, "page": 1, "box": box, "size": size}
    return line, [{"text": w, "page": 1, "box": b, "size": size, "geometry": "word", "parent_text": text}
                  for w, b in words]


def test_a_line_split_into_overlapping_ocr_boxes_loses_its_clipped_stem_and_repeated_colon():
    """PaddleOCR split "Invoice: DEMO-2026-002" into two overlapping crops (samples/invoice-2.pdf):
    the left one read the D's stem as "I", the right one read the colon again."""
    lines = _trim_split_lines([
        _ocr_line("Invoice:I", [76, 273, 160, 298],
                  [("Invoice", [76, 273, 139, 298]), (":", [147, 273, 151, 298]), ("I", [156, 273, 160, 298])]),
        _ocr_line(":DEMO-2026-002", [148, 273, 320, 298],
                  [(":", [148, 273, 152, 298]), ("DEMO-2026-002", [160, 273, 320, 298])]),
        _ocr_line("Supplier: I", [78, 319, 174, 345],
                  [("Supplier", [78, 319, 156, 345]), (": ", [156, 319, 170, 345]), ("I", [170, 319, 174, 345])]),
        _ocr_line("Lumena LLC", [174, 318, 445, 346],
                  [("Lumena", [174, 318, 244, 346]), ("LLC", [412, 318, 445, 346])]),
    ])
    text = spatial_text([line for line, _ in lines])
    assert text.splitlines() == ["Invoice: DEMO-2026-002", "Supplier: Lumena LLC"]
    assert [w["text"] for _, words in lines for w in words] == ["Invoice", ":", "DEMO-2026-002", "Supplier", ": ", "Lumena", "LLC"]
    assert lines[1][1][0]["parent_text"] == "DEMO-2026-002"


def test_a_printed_lone_i_before_a_spaced_neighbour_is_kept():
    lines = _trim_split_lines([
        _ocr_line("Class I", [76, 273, 160, 298], [("Class", [76, 273, 130, 298]), ("I", [150, 273, 154, 298])]),
        _ocr_line("Grade", [170, 273, 220, 298], [("Grade", [170, 273, 220, 298])]),
    ])
    assert lines[0][0]["text"] == "Class I"
