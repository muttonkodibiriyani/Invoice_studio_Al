"""Invoice date as printed and as ISO: column headings, and null whenever day and month could swap."""
from app.layout_extract import extract_invoice


def _row(y, *cells):
    """Word boxes on one row: (x, text) pairs, each word 10 high."""
    return [
        {"text": text, "box": [x, y, x + 9 * len(text), y + 10], "size": [600, 800], "page": 1}
        for x, text in cells
    ]


def _heading_strip(invoice_date, due_date="06/30/2026", delivery_date="06/02/2026"):
    boxes = _row(20, (40, "TAX"), (80, "INVOICE"))
    boxes += _row(60, (40, "Invoice"), (110, "Date"), (220, "Due"), (255, "Date"),
                  (360, "Delivery"), (440, "Date"))
    boxes += _row(78, (40, invoice_date), (220, due_date), (360, delivery_date))
    return boxes


def _read(boxes):
    return extract_invoice("TAX INVOICE\nInvoice No: T-100\n", boxes)


def test_date_under_its_column_heading_not_due_or_delivery():
    invoice = _read(_heading_strip("06/16/2026"))
    assert invoice["date_printed"] == "06/16/2026"
    assert invoice["date"] == "2026-06-16"


def test_ambiguous_column_date_is_printed_but_not_resolved():
    invoice = _read(_heading_strip("05/10/2026"))
    assert invoice["date_printed"] == "05/10/2026"
    assert invoice["date"] is None


def test_heading_with_no_date_below_reads_nothing():
    invoice = _read(_heading_strip("Pending"))
    assert invoice["date_printed"] is None
    assert invoice["date"] is None


def test_labelled_ambiguous_date_is_never_guessed():
    invoice = extract_invoice("TAX INVOICE\nInvoice No: T-100\nInvoice Date: 03/04/2026\n", None)
    assert invoice["date_printed"] == "03/04/2026"
    assert invoice["date"] is None


def test_compact_header_keeps_number_and_po_when_its_date_is_ambiguous():
    boxes = _row(20, (40, "TAX"), (80, "INVOICE"))
    for y, label, value in ((60, "No.", "T-200"), (76, "Date", "03/04/2026"), (92, "PO No", "4501")):
        boxes += _row(y, (400, label), (470, value))
    invoice = extract_invoice("TAX INVOICE", boxes)
    assert (invoice["number"], invoice["po"]) == ("T-200", "4501")
    assert invoice["date_printed"] == "03/04/2026"
    assert invoice["date"] is None
