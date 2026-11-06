"""Conservative invoice extraction for readable documents without a template.

This module deliberately extracts only facts printed on the document.  It does
not perform reference matching, approve an invoice, or derive totals and codes
from business rules.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from itertools import groupby
from math import radians, tan
from statistics import fmean, median
from typing import Any, Iterable

from app.models import Invoice, Line
from app.numbers import ISO_CURRENCIES, to_decimal


# An OCR reader may print a title's words with no space between them ("TaxInvoice"), so the
# qualifier binds to "invoice" with optional whitespace. A labelled invoice number or date is
# accepted as a title in its own right: a scan whose title glyphs were missed still names itself.
_STRONG_INVOICE = re.compile(
    r"(?im)(?:\b(?:tax|vat|gst|commercial|sales|debit|credit)\s*invoice\b|^\s*invoice\s*$"
    r"|\binvoice\s*(?:(?:no|number|date)\b|#)|^\s*invoice\s*:\s*(?=[A-Z0-9./_-]*[0-9])"
    r"|^\s*(?:bill|(?:credit|debit)\s*(?:note|memo)|(?:sales\s+)?return\s+note)\s*$)"
)
# A credit document prints what it takes back, often with no sign on it. Its amounts are left to review.
_CREDIT_TITLE = re.compile(
    r"(?im)^\s*(?:(?:tax|vat|gst)\s+)?(?:credit\s*(?:note|memo|invoice)|(?:sales\s+)?return\s+note)\b"
)
# "فاتورة", in the logical order an OCR model returns or the visual order a text layer holds.
_ARABIC_INVOICE = re.compile("فاتورة|ةروتاف")
_PACKING_TITLE = re.compile(r"(?i)\bpacking\s+(?:list|slip)\b")
_PURCHASE_ORDER_HEADING = re.compile(r"(?im)^\s*purchase\s+order(?:\s+no\b[^\n]*)?\s*$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_-]{0,79}$")
# The same identifier as printed, with the single spaces between its parts kept.
_SPACED_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_-]*(?: [A-Za-z0-9][A-Za-z0-9./_-]*){0,5}$")
_GTIN = re.compile(r"(?i)\b(?:EAN|GTIN|UPC)\s*[:#-]?\s*([0-9][0-9 -]{6,22}[0-9])\b")
# What separates a label from its value. A scan adds marks of its own between the two -
# "Invoice No: * 70001234", "Invoice Date ±+ 11.03.2024" - and a label whose value is
# unreachable behind them reads as no value at all.
_LABEL_GAP = r"\s*(?:[:#=-]\s*)?(?:[^\w\s]{1,2}\s*)?"
# "Reference Invoice No: 41" cites another invoice.
_NOT_CITED = "".join(rf"(?<!\b{word}\s)" for word in ("reference", "ref", "original", "against", "previous", "last", "related"))
_KNOWN_CURRENCY = "AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR|INR"
# What may follow a printed amount: its currency code, glued on or not ("1,234.50AED"), and then nothing that
# would make the digits part of something longer. A plain \\b would stop inside "1,234.50AED", at "1,234".
# A rate ("5 %"), a date ("12/03") or a count ("12 Items") is not an amount.
_COUNT_WORDS = r"items?|lines?|pcs|pieces?|units?|nos?|qty|cartons?|ctns?|boxes|box|products?|articles?"
# Nor is a figure whole when a group follows it after a space or an apostrophe ("676 507,80", "29'566.32"): a figure
# with its decimals printed already ended, but one without leaves the row to the word boxes, which can tell the gap.
_AMOUNT_END = (
    rf"(?!['’][0-9])(?:(?<=[.,][0-9])|(?<=[.,][0-9]{{2}})|(?<=[.,][0-9]{{3}})|(?!\s+[0-9]{{3}}(?![0-9])))"
    rf"\s*(?:{_KNOWN_CURRENCY})?(?![\w%]|[.,][0-9]|\s*%|\s*[/-]\s*[0-9]|\s+(?:{_COUNT_WORDS})\b)"
)
# The currency between a label and its figure.
# How a figure's money can be marked, glued on or not: the codes and the short forms and signs printed for them.
# A list, not any letters: "Tax NO30467" and "VAT INV1234.50" are references.
# Two letters mark money only on a figure with decimals: "Tax Amount SR47362" and "VAT Amount RO 2026" are references.
_LONG_MARK = (
    rf"{_KNOWN_CURRENCY}|JPY|EGP|CNY|CHF|CAD|AUD|NZD|SGD|HKD|MYR|THB|IDR|PHP|PKR|LKR|BDT|JOD|LBP|IQD|TRY|ZAR|NGN|KES|MAD"
    r"|TND|SEK|NOK|DKK|DHS|€|\$|£|¥"
)
_SHORT_MARK = "RO|KD|QR|SR|BD|DH"
_MONEY_MARK = rf"{_LONG_MARK}|{_SHORT_MARK}"
_CODE_GAP = rf"(?:(?:{_LONG_MARK})\s*|(?:{_SHORT_MARK})\s*(?=[0-9,]*\.[0-9]))?"
_EXCLUDING = r"(?:excl?|exl|excluding|exclusive|ex|pre|before|without)"
# The printed net, in the wordings that are not "net" or "subtotal". A gross or a line count never fits these,
# and neither does a row that only contains one. "Taxable" opens its row, or follows one of a few words: "Not Taxable
# Amount", "Zero Taxable Amount" and "VAT on Taxable Amount" are other figures, and they are too many to list.
_NET_WORDING = (
    r"(?:^[^\w]*(?:[^\x00-\x7f]+\s+)*(?:(?:total|sub\s*total|net|invoice)\s+)?taxable\s+(?:amount|value)"
    r"|(?:^|\s)(?<!\bnon\s)(?<!\bnon-)(?<!\bon\s)(?<!\bexempt\s)\b"
    r"(?:untaxed\s*amount|amount\s+untaxed|(?:total|amount)\s+before\s+(?:vat|tax)"
    rf"|\b{_EXCLUDING}\b[\s.-]*(?:of\s+)?(?:vat|tax|gst)\s+(?:total|amount)"
    rf"|\bamount\s+{_EXCLUDING}\b[\s.-]*(?:of\s+)?(?:vat|tax|gst)\b))"
)
# "Excl. Tax" is the net and "Incl. VAT" the gross: the tax word in them labels no tax figure.
_TAX_ONLY_NAMED = re.compile(
    rf"(?i)\b(?:{_EXCLUDING}|incl?|including|inclusive|after|with)\b[\s.-]*(?:of\s+)?(?:vat|tax|gst)\b"
)
# A tax figure's label, in _normal form, and nothing but a figure after it: "Tax Invoice", "Tax Date", "VAT No" and
# "GSTIN" are not it, and "VAT 100200300400003" or "Tax 2099" is a number beside the word, not an amount.
_TAX_AMOUNT_LABEL = re.compile(
    r"^(?:sales\s+|total\s+)?(?:vat|tax|gst)(?!\s*[0-9]{4,}\s*$)\s*(?:amount|amt|total)?\s*"
    rf"(?:[^\x00-\x7f]+\s+)*(?:(?:in\s+)?(?:{_MONEY_MARK.lower()})\s*)?[0-9]"
)


@dataclass(frozen=True)
class _Word:
    text: str
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    page_width: float
    page_height: float
    # The OCR text line this word was cut from, when the reader reports one.
    line: str | None = None

    @property
    def x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def y(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)


@dataclass(frozen=True)
class _Columns:
    item: float | None
    description: float
    uom: float | None
    qty: float
    price: float
    price_right: float
    serial: float | None = None
    net_bounds: tuple[float, float] | None = None
    tax_bounds: tuple[float, float] | None = None
    # Where one column stops and the next begins, read off the rows under the heading.
    splits: tuple[float, ...] | None = None


@dataclass
class _DraftLine:
    sku: str | None
    description: str | None
    uom: str | None
    qty: Decimal
    price: Decimal | None
    evidence_parts: list[str]
    page: int
    last_y: float
    gtin: str | None = None
    net_amount: Decimal | None = None
    tax_amount: Decimal | None = None


# Currencies a page names for something other than the invoice.
_OTHER_CURRENCY = re.compile(r"(?i)\b(?:foreign|reporting|beneficiary|remit\w*|settlement|bank|a/c|account)\s+currency\b")
_NET_NAME = re.compile(r"(?i)\b(?:net|sub[\s-]*total|taxable)\b")
_NOT_NET = re.compile(r"(?i)\b(?:due|payable|incl\w*|gross|with\s+(?:vat|tax|gst)|qty|quantity|items?|lines?|pcs|pieces|units?"
                      r"|pages?|non[\s-]*tax\w*|exempt|grand|غير|معف\w*)\b|^\W*total\W*$")
_TAX_NAME = re.compile(r"(?i)\b(?:vat|tax|gst)\b")
_NOT_TAX = re.compile(r"(?i)\b(?:amount\s+(?:due|payable)|withholding|wht|service|tourism|items?|qty|quantity|lines?)\b")
_COUNT_HEADING = re.compile(r"(?i)\b(?:items?|qty|quantity|lines?|pcs|pieces|units?)\b")
_RATE_LIKE = 25


_EDGE = 0.01


def _at_page_edge(word: _Word) -> bool:
    return word.x0 < word.page_width * _EDGE or word.x1 > word.page_width * (1 - _EDGE)


def _compact_value(value: str) -> str:
    return re.sub(r"[^\w]", "", value).casefold()


def _is_cut(value: str | None, cut: list[str]) -> bool:
    if not value:
        return False
    compact = _compact_value(value)
    return any(piece and (compact.startswith(piece) or compact.endswith(piece)) for piece in cut)


def _worded_figures(row: str) -> list[tuple[str, str]]:
    """Each figure of the row, with the words printed between it and the figure before."""

    out, start = [], 0
    for match in re.finditer(r"[0-9][0-9,.'’ ]*[0-9]|[0-9]", row):
        out.append((row[start : match.start()], match[0].strip()))
        start = match.end()
    return out


def _printed_as_other(value: Decimal, rows: list[str], name: re.Pattern[str], other: re.Pattern[str]) -> bool:
    """Whichever wording read the figure, the page says it is something else: every wording printed on it says so,
    it is a year, or it is the tail of a longer figure ("VAT 33 825.39"), or it stands under a heading of counts."""

    def is_value(figure: str) -> bool:
        return any(_parse_decimal(part) == value for part in {figure, *figure.split()})

    worded = [
        (words, figure) for row in rows for words, figure in _worded_figures(row) if re.search("[^\\W\\d_]", words)
    ]
    for words, figure in worded:
        spaced = re.fullmatch(r"([0-9]{1,3})[ '’](.+)", figure)
        if name.search(words) and spaced and _parse_decimal(spaced[2]) == value and int(spaced[1]) > _RATE_LIKE:
            return True
    holding = [(words, figure) for words, figure in worded if is_value(figure)]
    named = [(words, figure) for words, figure in holding if name.search(words)]
    if 1900 <= value <= 2099 and any(
        name.search(row) and figure == format(value, "f") for row in rows for _, figure in _worded_figures(row)
    ):
        return True
    if named:
        return all(other.search(words) for words, _ in holding)
    return any(
        name.search(above) and _COUNT_HEADING.search(above) and any(is_value(part) for part in row.split())
        and not re.search("[^\\W\\d_]", row)
        for above, row in zip(rows, rows[1:])
    )


def unproven_readings(text: str, boxes: list[dict[str, Any]] | None) -> dict[str, Any]:
    """{field: value} for what this page's wording does not prove: the value stands, a person checks it."""

    weak: set[str] = set()
    invoice = extract_invoice(text, boxes, weak) or {}
    return {field: invoice[field] for field in weak if invoice.get(field) not in (None, "")}


def extract_invoice(text: str, boxes: list[dict[str, Any]] | None, weak: set[str] | None = None) -> dict[str, Any] | None:
    """Extract visible invoice facts from OCR/native text and word boxes.

    A strong invoice title is required.  Conflicting labels and ambiguous dates
    are left unset, and line totals are never used to invent header totals.
    The result is shaped and validated by :class:`app.models.Invoice`.
    Fields read from a wording that proves little are added to ``weak``.
    """

    weak = set() if weak is None else weak

    source_text = text if isinstance(text, str) else ""
    words = _normalise_words(boxes or [])
    rows = _group_all_rows(words)
    reconstructed = "\n".join(_row_text(row) for _, row in rows)
    searchable = "\n".join(part for part in (source_text, reconstructed) if part)

    if _is_purchase_order_document(searchable):
        return None
    if not _has_invoice_heading(searchable):
        # No title on the word list. A priced table the page itself confirms still says invoice,
        # unless the document calls itself something else.
        from .arithmetic_lines import names_another_document, read_lines

        if names_another_document(searchable) or not read_lines(words):
            return None

    lines = [line.strip() for line in searchable.splitlines() if line.strip()]
    source_lines = [line.strip() for line in source_text.splitlines() if line.strip()]
    number = _unique_identifier(
        lines,
        (
            re.compile(
                r"(?i)" + _NOT_CITED + r"\b(?:invoice|inv|document|doc)\s*(?:no\.?|number|#)" + _LABEL_GAP +
                r"([A-Z0-9][A-Z0-9./_-]{0,79})"
            ),
            # A title that carries the number: "Tax Invoice SI/2026/0101", alone on its line.
            re.compile(r"(?i)^\s*tax\s+invoice\s+([A-Z0-9][A-Z0-9./_-]{2,79})\s*$"),
            # "Invoice: DEMO-2026-001": the label alone, opening its line ("Reference invoice: ..."
            # cites another invoice), its value an identifier of three or more.
            re.compile(r"(?i)^\s*invoice\s*:\s*([A-Z0-9][A-Z0-9./_-]{2,79})"),
        ),
    )
    labeled_number = _spatial_joined_identifier(
        rows,
        {"tax invoice number", "tax invoice no", "invoice number", "invoice no",
         "invoice#", "tax invoice#"},
    )
    if labeled_number is not None:
        number = labeled_number
    elif number is None:
        number = _spatial_invoice_number(rows)
    compact_header = _spatial_compact_header(rows)
    if number is None:
        number = compact_header.get("number")
    # Party names are read only from invoice pages: a packing list in the same
    # file labels its own parties beside its own titles.
    pages = {page for page, _ in rows}
    packing_pages = {
        page for page in pages
        if is_packing_page("\n".join(_row_text(row) for row_page, row in rows if row_page == page))
    }
    party_rows, party_lines = rows, lines
    if packing_pages and packing_pages != pages:
        party_rows = [(page, row) for page, row in rows if page not in packing_pages]
        party_lines = [text for text in (_row_text(row).strip() for _, row in party_rows) if text]
    supplier_name = _unique_text(
        party_lines,
        (
            re.compile(r"(?i)\b(?:supplier(?:\s+name)?|seller\s+name)\s*:\s*(.{1,160})$"),
        ),
    )
    buyer_name = _spatial_buyer_name(party_rows) or _extract_buyer_name(party_lines)
    supplier_name = (
        _spatial_party_name(
            party_rows, {"supplier", "supplier name", "sold by", "seller", "vendor", "supplied by", "billed by", "billed from"}
        )
        or supplier_name
    )
    if supplier_name is None:
        # A person, an office or a route prints these too.
        supplier_name = _spatial_party_name(party_rows, {"from", "issued by"})
        if supplier_name is None:
            supplier_name = _letterhead_supplier(party_rows)
            if supplier_name is not None and _LOOSE_FORM_END.search(supplier_name):
                weak.add("supplier_name")
            supplier_name = supplier_name or _footer_repeated_letterhead(party_rows, buyer_name)
        else:
            weak.add("supplier_name")
    po = _unique_identifier(
        lines,
        (
            re.compile(
                r"(?i)\bref\s*:\s*lpo\s*[-:#]?\s*([A-Z0-9][A-Z0-9./_-]{0,79})"
            ),
            re.compile(
                r"(?i)\b(?:lpo|po|purchase\s+order)\s*(?:no\.?|number|#)?\s*[:#-]\s*"
                r"(?:lpo\s*[-:#]?\s*)?([A-Z0-9][A-Z0-9./_-]{0,79})"
            ),
            re.compile(
                r"(?i)\b(?:lpo|po|purchase\s+order)\s+(?:no\.?|number|#)\s*[:#-]?\s+"
                r"([A-Z0-9][A-Z0-9./_-]{0,79})"
            ),
            # Labels with no colon: "Customer PO 70801", "Order Ref 70522", "LPO Ref LPO-77310".
            re.compile(
                r"(?i)\b(?:customer\s+l?po|order\s+ref(?:erence)?|l?po\s+ref)\s*(?:no\.?|number|#)?"
                r"\s*[:#-]?\s*(?:l?po\s*[-:#]?\s*)?([A-Z0-9][A-Z0-9./_-]{0,79})"
            ),
        ),
    )
    if po is None:
        po = _spatial_labeled_identifier(rows, {"lpo no", "lpo number", "po no", "po number"})
    if po is None:
        po = _identifier_below_label(
            rows, {"purchase order", "purchase order no", "lpo no", "lpo number", "po no", "po number"}
        )
    invoice_date = _extract_date(lines)
    if po is None:
        po = compact_header.get("po")
    if invoice_date is None and compact_header.get("date"):
        invoice_date = _parse_date(compact_header["date"])
    if invoice_date is None:
        invoice_date = _spatial_labeled_date(rows)
    column_date = _column_labeled_date(rows)
    if invoice_date is None and column_date is not None:
        invoice_date = _parse_date(column_date)
    currency = _extract_currency([line for line in lines if not _OTHER_CURRENCY.search(line)]) or _currency_under_label(rows)
    net_patterns = (
            re.compile(
                r"(?i)^\s*net\s+total\s*(?::\s*)?" + _CODE_GAP +
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*$"
            ),
            re.compile(
                r"(?i)^\s*net\s+amount\s*(?::\s*)?" + _CODE_GAP +
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*$"
            ),
            re.compile(
                r"(?i)^\s*sub\s*total\s*(?::\s*)?" + _CODE_GAP +
                r"([0-9][0-9,]*(?:\.[0-9]+)?)" + _AMOUNT_END + r"\s*$"
            ),
            # Ends with the row, so "Taxable Amount 100.00 VAT 5.00" is left to the word boxes.
            re.compile(
                r"(?i)" + _NET_WORDING + r"\s*(?::\s*)?" + _CODE_GAP +
                r"([0-9][0-9,]*(?:\.[0-9]+)?)" + _AMOUNT_END + r"\s*$"
            ),
        )
    net = _extract_money(source_lines, net_patterns)
    if net is None:
        net = _extract_money(lines, net_patterns)
    tax_patterns = (
            re.compile(
                r"(?i)(?:^|\s)(?:tax\s+amount|vat\s+(?:amount|amt\.?))\s*(?::\s*)?"
                + _CODE_GAP + r"([0-9][0-9,]*(?:\.[0-9]+)?)" + _AMOUNT_END
            ),
            re.compile(
                r"(?i)^\s*tax\s*\(\s*[0-9]+(?:\.[0-9]+)?\s*%\s*\)\s*:?[ \t]*"
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*$"
            ),
            # The rate ends in a % or a space: without one, "VAT 456.78" splits into a rate and an "8".
            re.compile(
                r"(?i)^\s*(?:vat|tax)\s*[-:]?\s*[0-9]{1,2}(?:\.[0-9]+)?(?:\s*%\s*|\s+(?![0-9]{3},[0-9]{1,2}\b))"
                + _CODE_GAP + r"([0-9][0-9,]*(?:\.[0-9]+)?)" + _AMOUNT_END + r"\s*$"
            ),
            # "VAT 225.00" with no rate. The decimals tell the amount from a bare rate ("VAT 5").
            re.compile(
                r"(?i)^\s*(?:vat|tax)\s*[-:]?\s*" + _CODE_GAP +
                r"([0-9][0-9,]*\.[0-9]{2,3})" + _AMOUNT_END + r"\s*$"
            ),
            # "VAT Total 118.75" on its own row, whatever the scan printed in front of it.
            re.compile(
                r"(?i)(?:^|\s)(?:vat|tax|gst)\s+total\s*(?::\s*)?" + _CODE_GAP +
                r"([0-9][0-9,.]*)\s*$"
            ),
        )
    tax = _extract_money([_TAX_ONLY_NAMED.sub(" ", line) for line in source_lines], tax_patterns)
    if tax is None:
        tax = _extract_money([_TAX_ONLY_NAMED.sub(" ", line) for line in lines], tax_patterns)
    spatial_net, spatial_tax = _extract_spatial_totals(words)
    if net is None:
        net = spatial_net
    if tax is None:
        tax = spatial_tax
    # Whatever the wordings above missed, from the labels the page prints (app.header_labels).
    from app.header_labels import read_labels

    labelled = read_labels(rows)
    earlier = {"number": number, "po": po, "date": invoice_date, "tax": tax, "supplier": supplier_name}
    weak.update("supplier_name" if field == "supplier" else field for field in labelled["weak"] if earlier.get(field, 0) is None)
    number = number if number is not None else labelled.get("number")
    # A word after the value that no label ends: it may be the value's own or the page's next word.
    if number and _RUNS_ON.search(number):
        weak.add("number")
    po = po if po is not None else labelled.get("po")
    if po and _RUNS_ON.search(po):
        weak.add("po")
    # A scan reads the O of a printed PO as a zero, and cuts the value at its marks.
    if po:
        po = re.sub(r"^(L?)P0(?=[-/.\s])", r"\1PO", re.sub(r"\s*([-/])\s*", r"\1", po))
    invoice_date = invoice_date if invoice_date is not None else labelled.get("date")
    currency = currency if currency is not None else labelled.get("currency")
    tax = tax if tax is not None else labelled.get("tax")
    supplier_name = supplier_name if supplier_name is not None else labelled.get("supplier")
    extracted_lines = _extract_table_lines(words)
    # A scan can put a stray mark before the net ("Net total: 8 800.00"). Its last amount is
    # the net only when the barcode rows add up to it exactly; "8 800.00" may be 8,800 otherwise.
    # Each mark ends in a space and only [^0-9]* takes the spaces and colon after the label,
    # so a long run of digits or spaces is read once, not backtracked over.
    candidates = [net] if net is not None else [
        _parse_decimal(match[1]) for line in source_lines
        if (match := re.match(r"(?i)^\s*net\s+total[^0-9]*(?:[0-9]+ +)*([0-9]+\.[0-9]{2})\s*$", line))
    ]
    for candidate in candidates:
        if extracted_lines or candidate is None:
            break
        extracted_lines = _barcode_rows(source_lines, candidate) or _barcode_rows(
            [_row_text(row).strip() for _, row in rows], candidate
        )
        if extracted_lines:
            net = candidate
    if net is None and "net" in labelled["weak"]:
        weak.add("net")
    net = net if net is not None else labelled.get("net")
    wording = [*source_lines, *(_row_text(row).strip() for _, row in rows)]
    if net is not None and _printed_as_other(net, wording, _NET_NAME, _NOT_NET):
        net = None
    if tax is not None and _printed_as_other(tax, wording, _TAX_NAME, _NOT_TAX):
        tax = None
    # A word the page's edge touches was cut there: "1126" of a longer figure, the end of a name.
    cut = [_compact_value(word.text) for word in words if _at_page_edge(word)]
    number, po, supplier_name = (
        None if _is_cut(value, cut) else value for value in (number, po, supplier_name)
    )
    net = None if net is not None and _is_cut(format(net, "f"), cut) else net
    tax = None if tax is not None and _is_cut(format(tax, "f"), cut) else tax
    # "1,200.00 CR" is a credit: whichever wording read it, its sign is left to review.
    credit_document = bool(_CREDIT_TITLE.search(searchable))
    net = None if credit_document or net in labelled["credits"] else net
    tax = None if credit_document or tax in labelled["credits"] else tax
    # No tax exceeds what it is charged on: one of the two figures is misread, and nothing says which.
    if net is not None and tax is not None and to_decimal(tax) > to_decimal(net) > 0:
        net = tax = None
    # No heading named the columns. Only then may the rows' own arithmetic find the table.
    if not extracted_lines:
        from .arithmetic_lines import names_another_document, read_lines

        if not names_another_document(searchable):
            extracted_lines = read_lines(words)

    invoice = Invoice(
        number=number,
        supplier_name=supplier_name,
        buyer_name=buyer_name,
        # These are internal/canonical fields and must come from reference review.
        seller=None,
        site=None,
        buyer=None,
        po=po,
        location=None,
        date=invoice_date,
        date_printed=_extract_printed_date(source_lines) or _extract_printed_date(lines) or column_date
        or compact_header.get("date"),
        currency=currency,
        origin=None,
        market=None,
        taxCode=None,
        net=net,
        tax=tax,
        lines=extracted_lines,
    )
    if not any(
        (
            invoice.number,
            invoice.supplier_name,
            invoice.buyer_name,
            invoice.po,
            invoice.date,
            invoice.currency,
            invoice.net is not None,
            invoice.tax is not None,
            invoice.lines,
        )
    ):
        return None
    return invoice.model_dump(mode="json")


def _has_invoice_heading(text: str) -> bool:
    return bool(_STRONG_INVOICE.search(text) or _ARABIC_INVOICE.search(unicodedata.normalize("NFKC", text)))


def is_packing_page(page_text: str) -> bool:
    """A packing list or slip page that carries no invoice title of its own."""

    return bool(_PACKING_TITLE.search(page_text)) and not _has_invoice_heading(page_text)


def _is_purchase_order_document(text: str) -> bool:
    """Reject a purchase-order title that precedes any invoice title."""

    purchase = _PURCHASE_ORDER_HEADING.search(text)
    if purchase is None:
        return False
    invoice = _STRONG_INVOICE.search(text)
    return invoice is None or purchase.start() < invoice.start()


def _ruled_cells(text: str, x0: float, x1: float) -> list[tuple[str, float, float]]:
    """A table ruled in text prints its bars against the cells: "|Goods", "184.6|", "Measur|Supplied". A lone bar stays."""
    if "|" not in text or not text.strip("|"):
        return [(text, x0, x1)]
    each = (x1 - x0) / len(text)
    return [
        (match.group(), x0 + match.start() * each, x0 + match.end() * each)
        for match in re.finditer(r"[^|]+", text)
    ]


def _normalise_words(boxes: Iterable[dict[str, Any]], *, max_page: int = 20) -> list[_Word]:
    words: list[_Word] = []
    for raw in boxes:
        if not isinstance(raw, dict):
            continue
        value = raw.get("text")
        bounds = raw.get("box")
        size = raw.get("size")
        if not isinstance(value, str) or not value.strip():
            continue
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            continue
        try:
            page = int(raw.get("page", 1))
            x0, y0, x1, y1 = (float(part) for part in bounds)
            page_width, page_height = (float(part) for part in size)
        except (TypeError, ValueError, OverflowError):
            continue
        if not 1 <= page <= max_page or page_width <= 0 or page_height <= 0:
            continue
        if not all(map(_finite, (x0, y0, x1, y1, page_width, page_height))):
            continue
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        coordinate_system = str(raw.get("coordinate_system", "")).lower()
        if "bottom" in coordinate_system:
            y0, y1 = page_height - y1, page_height - y0
        parent = raw.get("parent_text")
        for piece, piece_x0, piece_x1 in _ruled_cells(" ".join(value.split()), x0, x1):
            words.append(
                _Word(
                    text=piece,
                    page=page,
                    x0=piece_x0,
                    y0=y0,
                    x1=piece_x1,
                    y1=y1,
                    page_width=page_width,
                    page_height=page_height,
                    line=" ".join(parent.split()) if isinstance(parent, str) and parent.strip() else None,
                )
            )
    return _deskew(words)


def reading_rows(
    boxes: Iterable[dict[str, Any]], *, max_page: int = 20
) -> list[tuple[int, list[_Word]]]:
    """The OCR boxes grouped into the rows they are printed on, in reading order.

    One page tilt measurement and one row grouping serve every reader, so the plain
    text a template parses describes the same rows the table reader works from.
    """

    return _group_all_rows(_normalise_words(boxes, max_page=max_page))


def row_text(row: list[_Word]) -> str:
    """One printed row as text, left to right."""

    return _row_text(row)


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


# A scanned page is rarely square to the glass. A fifth of a degree of tilt is enough to put a
# printed row's left-hand text in one row and its right-hand figures in the next, which leaves
# the table unreadable, so the tilt is measured and taken out before anything is grouped.
_SKEW_DEGREES = 3.0
_SKEW_STEPS = 60
# Only correct a tilt that packs the words into measurably tighter rows than no correction at all.
_SKEW_GAIN = 1.02


def _deskew(words: list[_Word]) -> list[_Word]:
    """Take each page's tilt out of its word coordinates; the text and the order are untouched."""

    corrected: list[_Word] = []
    for page in sorted({word.page for word in words}):
        page_words = [word for word in words if word.page == page]
        slope = _page_slope(page_words)
        if not slope:
            corrected.extend(page_words)
            continue
        anchor = median(word.x for word in page_words)
        for word in page_words:
            shift = (word.x - anchor) * slope
            corrected.append(replace(word, y0=word.y0 - shift, y1=word.y1 - shift))
    return corrected


def _tilt_points(words: list[_Word]) -> list[tuple[float, float]]:
    """One point per printed line, for measuring a page's tilt.

    Some readers give every word of a detected line the whole line's box, so those words
    share one y and say nothing about where the line sits at each end of the page. Counted
    once per word, such a line outvotes the very tilt it is evidence of, and a photo held
    crooked reads as square.
    """

    points: list[tuple[float, float]] = []
    for _key, group in groupby(words, key=lambda word: word.line or id(word)):
        run = list(group)
        points.append((fmean(word.x for word in run), fmean(word.y for word in run)))
    return points


def _row_packing(points: list[tuple[float, float]], slope: float, anchor: float, band: float) -> int:
    """How tightly a tilt packs the printed lines into rows: largest when each row shares one band.

    Two binnings half a band apart are scored and the better taken, so the answer does not
    depend on where a row happens to fall against a bin edge.
    """

    best = 0
    for offset in (0.0, band / 2):
        counts: dict[int, int] = {}
        for x, y in points:
            key = int((y - (x - anchor) * slope + offset) // band)
            counts[key] = counts.get(key, 0) + 1
        best = max(best, sum(count * count for count in counts.values()))
    return best


def _page_slope(words: list[_Word]) -> float:
    """The page's tilt as dy/dx, or 0.0 when the page is square or there is too little text."""

    if len(words) < 40:
        return 0.0
    points = _tilt_points(words)
    if len(points) < 12:
        return 0.0
    band = max(2.0, median(word.height for word in words) / 2)
    anchor = median(x for x, _y in points)
    limit = tan(radians(_SKEW_DEGREES))
    square = _row_packing(points, 0.0, anchor, band)
    best_slope, best_score = 0.0, square
    for step in range(-_SKEW_STEPS, _SKEW_STEPS + 1):
        if step == 0:
            continue
        slope = limit * step / _SKEW_STEPS
        score = _row_packing(points, slope, anchor, band)
        if score > best_score:
            best_slope, best_score = slope, score
    return best_slope if best_score >= square * _SKEW_GAIN else 0.0


def _group_all_rows(words: list[_Word]) -> list[tuple[int, list[_Word]]]:
    grouped: list[tuple[int, list[_Word]]] = []
    for page in sorted({word.page for word in words}):
        for row in _group_page_rows([word for word in words if word.page == page]):
            grouped.append((page, row))
    return grouped


def _group_page_rows(words: list[_Word]) -> list[list[_Word]]:
    if not words:
        return []
    tolerance = max(3.0, median(word.height for word in words) * 0.6)
    rows: list[list[_Word]] = []
    centres: list[float] = []
    for word in sorted(words, key=lambda item: (item.y, item.x)):
        if rows and abs(word.y - centres[-1]) <= tolerance:
            rows[-1].append(word)
            centres[-1] = sum(item.y for item in rows[-1]) / len(rows[-1])
        else:
            rows.append([word])
            centres.append(word.y)
    return [
        sorted(line, key=lambda item: item.x)
        for row in rows
        for line in _unstack(row)
    ]


def _unstack(row: list[_Word]) -> list[list[_Word]]:
    """Split a row that is really two stacked printed lines the grouping pulled together.

    One printed line never puts two words over the same part of the page, so words that
    overlap horizontally come from different lines. Two label columns printed close
    together are read as one row otherwise, and their values end up in each other's fields.
    """

    if len(row) < 4 or not _overlapping(row):
        return [row]
    ordered = sorted(row, key=lambda word: word.y)
    gaps = [
        (ordered[index + 1].y - ordered[index].y, index + 1)
        for index in range(len(ordered) - 1)
    ]
    widest, cut = max(gaps)
    if widest <= 0:
        return [row]
    halves = [ordered[:cut], ordered[cut:]]
    if any(_overlapping(half) for half in halves):
        return [row]
    return [half for half in halves if half]


def _overlapping(row: list[_Word]) -> bool:
    """Two of the row's words cover the same x span, by more than half the narrower word."""

    ordered = sorted(row, key=lambda word: word.x0)
    for left, right in zip(ordered, ordered[1:]):
        shared = left.x1 - right.x0
        narrower = min(left.x1 - left.x0, right.x1 - right.x0)
        if shared > max(1.0, narrower * 0.5):
            return True
    return False


def _row_text(row: list[_Word]) -> str:
    return " ".join(word.text for word in sorted(row, key=lambda item: item.x)).strip()


def _unique_identifier(
    lines: list[str], patterns: tuple[re.Pattern[str], ...]
) -> str | None:
    values: list[str] = []
    for line in lines:
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                value = match.group(1).strip().rstrip(".,:;")
                # A label followed by the next label ("Invoice No.  Tax Invoice
                # Date") is not a value; printed identifiers carry a digit.
                if _SAFE_IDENTIFIER.fullmatch(value) and any(char.isdigit() for char in value) and not _is_date(value):
                    values.append(value)
                break
    return _only_distinct(values)


def _is_date(value: str) -> bool:
    """A date printed where an identifier belongs is the next field's value, whatever the label before it says."""

    return bool(re.fullmatch(r"[0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4}", value.strip())) or _parse_date(value) is not None


def _spatial_invoice_number(rows: list[tuple[int, list[_Word]]]) -> str | None:
    """Read one prominent identifier printed beside an invoice heading."""

    values: list[str] = []
    for row_index, (page, row) in enumerate(rows):
        if not re.search(r"(?i)\b(?:tax|commercial|sales|debit|credit)\s*invoice\b", _row_text(row)):
            continue
        heading_words = [word for word in row if "invoice" in _normal(word.text) or _normal(word.text) == "tax"]
        if not heading_words:
            continue
        heading_right = max(word.x1 for word in heading_words)
        candidates: list[str] = []
        for word in row:
            candidate = word.text.strip().replace(" ", "")
            if word.x0 <= heading_right or not _SAFE_IDENTIFIER.fullmatch(candidate):
                continue
            if len(candidate) < 4 or not any(char.isdigit() for char in candidate) or _is_date(candidate):
                continue
            candidates.append(candidate)
        if not candidates and row_index:
            previous_page, previous = rows[row_index - 1]
            gap = _row_y(row) - _row_y(previous)
            heading_height = max(word.height for word in heading_words)
            if previous_page == page and 0 < gap <= max(100.0, heading_height * 6.0):
                for word in previous:
                    candidate = word.text.strip().replace(" ", "")
                    if word.x0 <= heading_right or not _SAFE_IDENTIFIER.fullmatch(candidate):
                        continue
                    if len(candidate) < 4 or not any(char.isdigit() for char in candidate) or _is_date(candidate):
                        continue
                    candidates.append(candidate)
        if len(set(candidates)) == 1:
            values.extend(candidates)
    return _only_distinct(values)


def _spatial_labeled_date(rows: list[tuple[int, list[_Word]]]) -> str | None:
    values: list[str] = []
    labels = {"date", "document date", "invoice date"}
    for _, row in rows:
        for start in range(len(row)):
            for width in (2, 1):
                end = start + width
                if end > len(row):
                    continue
                phrase = " ".join(_normal(word.text) for word in row[start:end])
                if phrase not in labels:
                    continue
                if phrase == "date" and start and _QUALIFIED_DATE.search(_normal(row[start - 1].text)):
                    continue
                label_right = max(word.x1 for word in row[start:end])
                for candidate_word in row[end:]:
                    if candidate_word.x0 < label_right:
                        continue
                    if candidate_word.x0 - label_right > row[0].page_width * 0.25:
                        break
                    parsed = _parse_date(candidate_word.text)
                    if parsed is not None:
                        values.append(parsed)
                    break
                break
    return _only_distinct(values)


# A date as printed: numeric in any order, or with a month name.
_PRINTED_DATE = re.compile(
    r"(?i)\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}(?:st|nd|rd|th)?[- /][A-Za-z]{3,9},?[- /]\d{2,4}"
    r"|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}"
)


def _column_labeled_date(rows: list[tuple[int, list[_Word]]]) -> str | None:
    """The date printed under an "Invoice Date" column heading, as printed.

    A header strip prints "Invoice Date  Due Date  Delivery Date" on one row and the dates on the row
    below, so the value is the one under the heading's own span: the due and delivery dates sit under
    theirs. Only the row just below counts.
    """

    values: list[str] = []
    for index, (page, row) in enumerate(rows):
        for start in range(len(row) - 1):
            if " ".join(_normal(word.text) for word in row[start:start + 2]) not in {"invoice date", "document date"}:
                continue
            label = row[start:start + 2]
            left, right = label[0].x0, label[-1].x1
            slack = label[0].height
            below = next(
                (other for other_page, other in rows[index + 1:index + 2] if other_page == page), None
            )
            if not below or below[0].y0 - label[0].y1 > 3 * slack:
                continue
            under = [word for word in below if left - slack <= word.x <= right + slack]
            printed = re.sub(r"\s*([-/.,])\s*", r"\1", " ".join(word.text for word in under)).strip()
            printed = re.sub(r",(?=\d{4})", ", ", printed)
            if _PRINTED_DATE.fullmatch(printed):
                values.append(printed)
    return _only_distinct(values)


def _spatial_labeled_identifier(
    rows: list[tuple[int, list[_Word]]], labels: set[str]
) -> str | None:
    """Read one identifier immediately to the right of an explicit label."""

    values: list[str] = []
    for _, row in rows:
        for start in range(len(row)):
            for width in (1, 2, 3):
                end = start + width
                if end > len(row):
                    continue
                label_words = row[start:end]
                if " ".join(_normal(word.text) for word in label_words) not in labels:
                    continue
                label_right = max(word.x1 for word in label_words)
                label_height = max(word.height for word in label_words)
                for candidate_word in row[end:]:
                    if candidate_word.x0 < label_right:
                        continue
                    if candidate_word.x0 - label_right > max(36.0, label_height * 5.0):
                        break
                    candidate = candidate_word.text.strip().replace(" ", "")
                    if _SAFE_IDENTIFIER.fullmatch(candidate) and any(
                        char.isdigit() for char in candidate
                    ):
                        values.append(candidate)
                    break
    return _only_distinct(values)


def _identifier_below_label(
    rows: list[tuple[int, list[_Word]]], labels: set[str]
) -> str | None:
    """Read the identifier printed under a header-table heading: "Purchase Order" over "70412".

    The value is the one word in the next row down whose span overlaps the heading's, so the
    cells of the neighbouring columns are never read for it.
    """

    values: list[str] = []
    for row_index, (page, row) in enumerate(rows):
        for start in range(len(row)):
            for width in (3, 2):
                end = start + width
                if end > len(row):
                    continue
                label_words = row[start:end]
                if _label_phrase(label_words) not in labels:
                    continue
                left = min(word.x0 for word in label_words)
                right = max(word.x1 for word in label_words)
                height = max(word.height for word in label_words)
                below = next(
                    (
                        next_row for next_page, next_row in rows[row_index + 1 : row_index + 3]
                        if next_page == page and 0 < _row_y(next_row) - _row_y(row) <= height * 4.0
                    ),
                    None,
                )
                if below is None:
                    break
                under = [word for word in below if word.x0 < right and word.x1 > left]
                if len(under) == 1:
                    candidate = under[0].text.strip().replace(" ", "")
                    candidate = re.sub(r"(?i)^l?po\s*[-:#]?(?=[0-9])", "", candidate)
                    if _SAFE_IDENTIFIER.fullmatch(candidate) and any(
                        character.isdigit() for character in candidate
                    ):
                        values.append(candidate)
                break
    return _only_distinct(values)


_CITING_WORDS = {"reference", "ref", "original", "against", "previous", "last", "related"}


def _spatial_joined_identifier(
    rows: list[tuple[int, list[_Word]]], labels: set[str]
) -> str | None:
    """Join adjacent OCR tokens after one explicit identifier label.

    OCR commonly returns ``ABC-`` and ``01`` as separate words. Only tokens
    touching the label's value run are joined, and the result must still be a
    short safe identifier containing a digit.
    """

    values: list[str] = []
    for _, row in rows:
        for start in range(len(row)):
            for width in (3, 2):
                end = start + width
                if end > len(row):
                    continue
                label_words = row[start:end]
                if _label_phrase(label_words) not in labels:
                    continue
                if start and _normal(row[start - 1].text) in _CITING_WORDS:
                    break
                label_right = max(word.x1 for word in label_words)
                label_height = max(word.height for word in label_words)
                pieces: list[str] = []
                sources: set[str] = set()
                previous_right = label_right
                for position, word in enumerate(row[end:], end):
                    if word.x0 < label_right:
                        continue
                    token = word.text.strip().replace(" ", "")
                    # The value runs along the line only as far as the next label or a date.
                    if pieces and (_starts_label(row, position) or _is_date(token)):
                        break
                    if token in {":", "#", "-"} and not pieces:
                        previous_right = word.x1
                        continue
                    # The gap from the label to its value is the one a form leaves between
                    # two columns; the gaps inside the value are the ones between its parts.
                    # A value right-aligned in a header box sits further off: up to a fifth of the page.
                    allowed = (
                        max(36.0, label_height * 5.0, word.page_width * 0.2) if not pieces
                        else max(12.0, label_height * 1.5)
                    )
                    if word.x0 - previous_right > allowed:
                        break
                    if not re.fullmatch(r"[A-Za-z0-9./_-]+", token):
                        break
                    pieces.append(token)
                    sources.add(word.line or "")
                    previous_right = word.x1
                candidate = "".join(pieces)
                # Words from a text layer are split where the page prints a space: "NWT 004512".
                if sources == {""} and _SPACED_IDENTIFIER.fullmatch(" ".join(pieces)):
                    candidate = " ".join(pieces)
                # The reader may report the printed line these words were cut from. When it
                # covers exactly them, keep its spacing: "ULTA TDM 300726", not "ULTATDM300726".
                printed = next(iter(sources)) if len(sources) == 1 else None
                if (
                    printed
                    and "".join(printed.split()) == candidate
                    and _SPACED_IDENTIFIER.fullmatch(printed)
                ):
                    candidate = printed
                if (
                    candidate
                    and len(candidate) <= 80
                    and (_SAFE_IDENTIFIER.fullmatch(candidate) or _SPACED_IDENTIFIER.fullmatch(candidate))
                    and any(character.isdigit() for character in candidate)
                    and not _is_date(candidate)
                ):
                    values.append(candidate)
                break
    return _only_distinct(values)


def _starts_label(row: list[_Word], index: int) -> bool:
    from app.header_labels import starts_label

    return starts_label(row, index)


def _label_remainder(words: list[_Word], labels: set[str], prefix: bool = True) -> str | None:
    """What a label box holds after its label, or "" when the label stands alone.

    A word-box reader returns "Payer", ":" and "101063" as three boxes, but a phrase reader
    returns them as one: "Payer : 101063". Matching only a box that is exactly the label
    finds no label at all on a phrase reader, so the party is never read. ``None`` means
    these words are not this label.

    A value only ever shares the one box its label was printed in, so ``prefix`` is set for a
    single box and cleared for a window of several: across two boxes "Payer : 101063" and
    "Invoice No:" read as one label whose value is the next field's name.
    """

    phrase = " ".join(_normal(word.text) for word in words)
    for label in sorted(labels, key=len, reverse=True):
        if phrase == label:
            return ""
        if prefix and phrase.startswith(label + " "):
            text = " ".join(" ".join(word.text for word in words).split())
            cut = re.sub(r"(?i)^\W*" + r"\W+".join(label.split()) + r"\W*", "", text, count=1)
            return cut if cut != text else ""
    return None


def _spatial_party_name(
    rows: list[tuple[int, list[_Word]]], labels: set[str]
) -> str | None:
    """Read a party name beside or immediately below an explicit label."""

    values: list[str] = []
    for row_index, (page, row) in enumerate(rows):
        for start in range(len(row)):
            # A label opens its own field. One a word-space after the previous word is part of a
            # sentence ("Not a real supplier invoice."), and what follows it is no party name.
            if start and row[start].x0 - row[start - 1].x1 < row[start].height * 0.6:
                continue
            for width in (2, 1):
                end = start + width
                if end > len(row):
                    continue
                label_words = row[start:end]
                remainder = _label_remainder(label_words, labels, prefix=width == 1)
                if remainder is None:
                    continue
                candidate = _clean_party_name(remainder) if remainder else None
                if candidate is None:
                    label_right = max(word.x1 for word in label_words)
                    same_row = [
                        word for word in row[end:]
                        if word.x0 >= label_right
                        and word.x0 - min(item.x0 for item in label_words)
                        <= row[0].page_width * 0.4
                    ]
                    candidate = _clean_party_name(_join_words(same_row))
                if candidate is None:
                    label_left = min(word.x0 for word in label_words)
                    label_y = _row_y(row)
                    label_height = max(word.height for word in label_words)
                    for next_page, next_row in rows[row_index + 1 :]:
                        if next_page != page:
                            continue
                        gap = _row_y(next_row) - label_y
                        if gap <= 0:
                            continue
                        if gap > max(50.0, label_height * 4.0):
                            break
                        nearby = [
                            word for word in next_row
                            if word.x0 >= label_left
                            and word.x0 - label_left <= row[0].page_width * 0.35
                        ]
                        candidate = _clean_party_name(_join_words(nearby))
                        if candidate is not None:
                            break
                if candidate is not None:
                    values.append(candidate)
                break
    return _only_distinct(values)


# Labels that name the party being invoiced.  "Customer" alone is left out: it also opens
# "Customer PO No.", and a second reading of one field makes both of them ambiguous.
_BUYER_LABELS = {
    "bill to", "billed to", "bill to party", "buyer", "payer", "invoice to", "invoiced to",
    "sold to", "customer name",
}
# Labels of the fields printed to the right of a party block.  They mark where the block
# stops, so an invoice number or date on the same rows is not read as part of the name.
_FIELD_LABELS = {
    "invoice", "invoice no", "invoice number", "invoice date", "tax invoice", "order no",
    "order number", "order date", "document no", "document date", "date", "due date",
    "delivery note", "lpo no", "lpo number", "po no", "po number", "page", "terms",
    "payment terms", "salesman", "currency", "trn", "vat no",
}


def _spatial_buyer_name(rows: list[tuple[int, list[_Word]]]) -> str | None:
    values: list[str] = []
    for row_index, (page, row) in enumerate(rows):
        label: tuple[int, int, str] | None = None
        for start in range(len(row)):
            for width in (2, 1):
                remainder = _label_remainder(
                    row[start : start + width], _BUYER_LABELS, prefix=width == 1
                )
                if remainder is not None:
                    label = start, start + width, remainder
                    break
            if label is not None:
                break
        if label is None:
            continue
        start, end, remainder = label
        # The name printed beside the label is the buyer; the rows below it are read only
        # when the label stands alone, so "Payer : 101063" falls through to the name under it.
        inline = _clean_party_name(remainder) if remainder else None
        if inline is not None:
            values.append(inline)
            continue
        label_left = row[start].x0
        right_boundary = row[0].page_width
        for position in range(end, len(row)):
            phrase = " ".join(
                _normal(word.text) for word in row[position : position + 2]
            )
            single = _normal(row[position].text)
            if (
                phrase in {"ship to", "deliver to"}
                or single in {"from", "supplier", "seller", "vendor", "remit to"}
                or phrase in _FIELD_LABELS
                or single in _FIELD_LABELS
            ):
                right_boundary = row[position].x0
                break
        # A word-box reader cuts "Bill to: Retail UAE" into boxes, so the name beside the label
        # is the words after it on the same row, up to the next field's label.
        beside = _clean_party_name(_join_words([word for word in row[end:] if word.x < right_boundary]))
        if beside is not None:
            values.append(beside)
            continue
        label_y = _row_y(row)
        label_height = max(word.height for word in row[start:end])
        for next_page, candidate_row in (item for item in rows[row_index + 1 :] if item[0] == page):
            gap = _row_y(candidate_row) - label_y
            if gap <= 0:
                continue
            if gap > max(90.0, label_height * 8.0):
                break
            candidate_words = [
                word for word in candidate_row
                if word.x >= label_left and word.x < right_boundary
            ]
            candidate = _clean_party_name(_join_words(candidate_words))
            if candidate is not None:
                values.append(candidate)
                break
    return _only_distinct(values)


_FORM_WORD = (
    r"(?:l\.?\s?l\.?\s?c|ltd|limited|inc|incorporated|plc|fz\s?llc|fze|fzco|w\.?\s?l\.?\s?l|gmbh|s\.?a\.?r\.?l"
    r"|b\.?v|n\.?v|s\.?p\.?a|pvt|private|pte|pjsc|psc|saog|saoc|b\.?s\.?c|k\.?s\.?c\.?c|est|establishment|corporation"
    r"|corp|co|company|s\.a|a\.?g|&\s*co)"
)
# A name carries its form whole: "Pvt Ltd" and "Co. Ltd" are one form, and a dot printed on it is part of it.
_COMPANY_FORM = re.compile(rf"(?i)\b{_FORM_WORD}\b(?:[\s.,]*\b{_FORM_WORD}\b)*\.?")
# These end plenty of sentences too: "Serving the Gulf Est.", "A Division of the Company".
_LOOSE_FORM_END = re.compile(r"(?i)(?<![\w.])(?:est|corp|co|company)\.?$")
_RUNS_ON = re.compile(r"[0-9]\S*\s+[A-Za-z]{2,}$")


_BUYER_LABEL_IN_ROW = re.compile(
    r"(?i)\b(?:bill(?:ed)?\s+to|sold\s+to|ship(?:ped)?\s+to|deliver(?:ed)?\s+to|buyer|customer|client|consignee)\b"
)


_NOTICE_WORDS = re.compile(r"\b(?:copy|confidential|thank|thanks|duplicate|document|page|signature|stamp)\b")
_ADDRESS_HINT = re.compile(
    r"(?i)\b(?:p\.?\s*o\.?\s*box|address|tel(?:ephone)?|phone|fax|e-?mail|www|street|road|building|floor|trn"
    r"|vat\s*(?:no|reg)|tax\s*(?:no|reg))\b|@|[·•|].*[·•|]"
)


def _letterhead_supplier(rows: list[tuple[int, list[_Word]]]) -> str | None:
    """The supplier read from the letterhead, for an invoice where no label names it.

    An invoice is issued by the party printed at the head of its own page, and plenty of
    suppliers print that name only in the logo, with no "Supplier:" label anywhere on the
    document. Only the top of an invoice page is read, and only a name that carries a company
    form, so an address line, a title or a buyer block is never taken for it. Whatever the
    letterhead prints after the company form - a second language, a licence number - is cut.
    """

    for _page, row in rows:
        if not row or _row_y(row) > row[0].page_height * 0.2:
            continue
        text = _row_text(row)
        # A party block at the head of the page names the buyer, not the issuer.
        if _BUYER_LABEL_IN_ROW.search(text):
            continue
        match = _COMPANY_FORM.search(text)
        if match is None:
            continue
        candidate = _clean_party_name(text[: match.end()])
        if candidate is not None and len(candidate) >= 6:
            return candidate
    return None


def _footer_repeated_letterhead(rows: list[tuple[int, list[_Word]]], buyer_name: str | None) -> str | None:
    """The issuer's name with no company form: it opens the page's top row and is a whole row at its foot.

    A name at both ends of the page proves little alone: a notice or an unlabelled buyer can repeat too. So it also
    has to carry a company form or have an address under it at either end. A title, a notice or the buyer's name is
    never taken for it.
    """

    def address_under(row: list[_Word], page: int, depth: float) -> bool:
        return any(
            below_page == page and _row_y(row) < _row_y(below) <= _row_y(row) + depth
            and _ADDRESS_HINT.search(_row_text(below))
            for below_page, below in rows
        )

    buyer = _normal(buyer_name or "")
    for page, foot in rows:
        if not foot or _row_y(foot) < foot[0].page_height * 0.75:
            continue
        name = _normal(_row_text(foot))
        if (
            len(name.split()) < 2 or re.search(r"[0-9]", name) or name == buyer or _has_invoice_heading(name)
            or _NOTICE_WORDS.search(name)
        ):
            continue
        for head_page, head in rows:
            if head_page != page or _row_y(head) > head[0].page_height * 0.2:
                continue
            for end in range(2, len(head) + 1):
                if _normal(_row_text(head[:end])) == name and (
                    _COMPANY_FORM.search(_row_text(head[:end]))
                    or address_under(head, page, head[0].page_height * 0.15)
                    or address_under(foot, page, foot[0].page_height * 0.05)
                ):
                    return _clean_party_name(_join_words(head[:end]))
    return None


def _spatial_compact_header(rows: list[tuple[int, list[_Word]]]) -> dict[str, str]:
    """Read an explicit aligned No./Date/PO header block.

    Bare ``No.`` and ``Date`` labels are too broad on their own.  They are
    accepted only when all three invoice labels share the same right-side
    column on one page and each has one visible value immediately to its right.
    """

    labels = {"no": "number", "date": "date", "po no": "po", "po number": "po"}
    records: list[tuple[int, str, float, float, str]] = []
    for page, row in rows:
        for start in range(len(row)):
            for width in (2, 1):
                end = start + width
                if end > len(row):
                    continue
                phrase = " ".join(_normal(word.text) for word in row[start:end])
                field = labels.get(phrase)
                if field is None:
                    continue
                if field == "date" and start and _normal(row[start - 1].text) == "due":
                    continue
                if field == "number" and start and _normal(row[start - 1].text) in {
                    "po", "lpo", "warehouse",
                }:
                    continue
                label_words = row[start:end]
                label_right = max(word.x1 for word in label_words)
                page_width = row[0].page_width
                candidate = next(
                    (
                        word.text.strip()
                        for word in row[end:]
                        if word.x0 >= label_right
                        and word.x0 - label_right <= page_width * 0.22
                    ),
                    None,
                )
                if candidate is None:
                    continue
                if field == "date":  # kept as printed: an ambiguous date still marks the block
                    parsed = candidate if _parse_date(candidate) or _PRINTED_DATE.fullmatch(candidate) else None
                else:
                    compact = candidate.replace(" ", "")
                    parsed = (
                        compact
                        if _SAFE_IDENTIFIER.fullmatch(compact)
                        and any(character.isdigit() for character in compact)
                        else None
                    )
                if parsed is not None:
                    records.append((page, field, label_words[0].x, page_width, parsed))
                break

    valid: list[dict[str, str]] = []
    for page, _field, anchor, page_width, _value in records:
        nearby = [
            record
            for record in records
            if record[0] == page
            and abs(record[2] - anchor) <= max(18.0, page_width * 0.04)
        ]
        values = {
            field: {value for _page, candidate_field, _x, _width, value in nearby
                    if candidate_field == field}
            for field in {"number", "date", "po"}
        }
        if all(len(field_values) == 1 for field_values in values.values()):
            valid.append({field: next(iter(field_values)) for field, field_values in values.items()})
    distinct = {tuple(sorted(candidate.items())) for candidate in valid}
    return dict(next(iter(distinct))) if len(distinct) == 1 else {}


def _unique_text(lines: list[str], patterns: tuple[re.Pattern[str], ...]) -> str | None:
    values: list[str] = []
    for line in lines:
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            value = " ".join(match.group(1).split()).strip(" :")
            if value and not _looks_dangerous(value):
                values.append(value[:160])
            break
    return _only_distinct(values)


def _extract_buyer_name(lines: list[str]) -> str | None:
    values: list[str] = []
    label = re.compile(r"(?i)^\s*(?:bill\s+to|buyer)\s*:?[ \t]*(.*)$")
    for index, line in enumerate(lines):
        match = label.match(line)
        if match is None:
            continue
        candidate = match.group(1)
        if not candidate and index + 1 < len(lines):
            candidate = lines[index + 1]
        cleaned = _clean_party_name(candidate)
        if cleaned is not None:
            values.append(cleaned)
    return _only_distinct(values)


def _clean_party_name(value: Any) -> str | None:
    candidate = " ".join(str(value or "").split()).strip(" :|-")
    # A form prints its own field labels beside the name: "Bill To: Company: ACME LLC".
    leading = (
        r"(?:bill\s+to|buyer|company(?:\s+name)?|customer(?:\s+name)?|"
        r"supplier(?:\s+name)?|seller(?:\s+name)?|vendor|sold\s+by|issued\s+by|supplied\s+by|billed\s+by|"
        r"billed\s+from|from)"
    )
    while re.match(rf"(?i)^{leading}\b(?!$)", candidate):
        shortened = re.sub(rf"(?i)^{leading}\b\s*:?[ \t|,-]*", "", candidate, count=1)
        if not shortened or shortened == candidate:
            break
        candidate = shortened
    # "CO. L.L.C" arrives from a word-box reader as "CO . L . L . C": the spacing is the
    # reader's, not the document's, and the name has to read back as it was printed.
    candidate = re.sub(r"(?<=\b[A-Za-z])\s*\.\s*(?=[A-Za-z]\b)", ".", candidate)
    candidate = re.sub(r"\s+([.,])", r"\1", candidate)
    candidate = re.split(
        r"(?i)\b(?:ship\s+to|deliver\s+to|p\.?\s*o\.?\s*box|address|tel(?:ephone)?|trn)\b",
        candidate,
        maxsplit=1,
    )[0].strip(" :|-,")
    if (
        not 2 <= len(candidate) <= 160
        or _looks_dangerous(candidate)
        or not any(character.isalpha() for character in candidate)
        or _normal(candidate) in {"bill to", "buyer", "ship to", "deliver to"}
    ):
        return None
    return candidate


def _only_distinct(values: Iterable[str]) -> str | None:
    distinct: dict[str, str] = {}
    for value in values:
        distinct.setdefault(value.casefold(), value)
    if len(distinct) != 1:
        return None
    return next(iter(distinct.values()))


def _extract_printed_date(lines: list[str]) -> str | None:
    """Preserve a labelled source date even when its locale is ambiguous."""
    date_pattern = r"(?:\d{1,4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,4}|\d{1,2}(?:st|nd|rd|th)?[- /][A-Za-z]{3,9},?[- /]\d{4})"
    # A gap starts where its run of spaces starts, so a long run is walked once, not from each space.
    label = re.compile(
        r"(?i)(?:^|(?<!\s)\s{2,}|\b(?:invoice|document)\s+)date" + _LABEL_GAP + r"(" + date_pattern + r")(?!\d)"
    )
    values = []
    for i, line in enumerate(lines):
        match = label.search(line)
        if match:
            values.append(match.group(1).strip())
        elif re.fullmatch(r"(?i)\s*(?:(?:tax\s+)?invoice\s+|document\s+)?date\s*(?::\s*)?", line) and i + 1 < len(lines):
            if re.fullmatch(date_pattern, lines[i + 1].strip()):
                values.append(lines[i + 1].strip())
        else:
            bare = re.fullmatch(r"(?i)\s*date\s*(?::\s*)?(" + date_pattern + r")\s*", line)
            if bare:
                values.append(bare.group(1).strip())
    normalized = {}
    for value in values:
        normalized.setdefault(re.sub(r"\s*([-/.])\s*", r"\1", value).casefold(), value)
    return next(iter(normalized.values())) if len(normalized) == 1 else None


# "Due Date", "Order Date": a date of something else, whichever line the invoice's own date stands on.
_QUALIFIED_DATE = re.compile(
    r"(?i)\b(?:due|delivery|order|po|lpo|ship(?:ping|ment)?|d[ie]spatch|expiry|payment|print(?:ed)?|run|report|posting"
    r"|valid(?:ity)?|value)\s*$"
)


def _extract_date(lines: list[str]) -> str | None:
    priorities = (
        re.compile(r"(?i)\bdocument\s+date" + _LABEL_GAP + r"([0-9A-Z./ -]{6,24})"),
        re.compile(r"(?i)\binvoice\s+date" + _LABEL_GAP + r"([0-9A-Z./ -]{6,24})"),
        re.compile(r"(?i)^\s*dated?" + _LABEL_GAP + r"([0-9A-Z./ -]{6,24})\s*$"),
        re.compile(
            r"(?i)(?<!report\srun\s)(?<!run\s)\bdate" + _LABEL_GAP + r"([0-9A-Z./ -]{6,24})"
        ),
    )
    for pattern in priorities:
        values: list[str] = []
        for line in lines:
            for match in pattern.finditer(line):
                if _QUALIFIED_DATE.search(line[: match.start()]):
                    continue
                parsed = _parse_date(match.group(1).strip())
                if parsed:
                    values.append(parsed)
                break
        if values:
            return _only_distinct(values)
    return None


def _full_year(value: str) -> int | None:
    """Expand a printed year; a two-digit year is read as this century."""
    try:
        year = int(value)
    except ValueError:
        return None
    if len(value.strip()) <= 2:
        # A two-digit year is a guess, so only accept it inside a plausible
        # invoicing window: anything else is left empty for review.
        year += 2000
        today = date.today()
        if not today.year - 10 <= year <= today.year + 1:
            return None
    if not 1900 <= year <= 2199:
        return None
    return year


def _build_date(year: str, month: str, day: str) -> str | None:
    """Build an ISO date from a printed year, alphabetic month name and day."""
    full_year = _full_year(year)
    if full_year is None:
        return None
    try:
        return date(full_year, _month_number(month), int(day)).isoformat()
    except (ValueError, TypeError):
        return None


def _parse_date(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value.strip()).rstrip(".,")
    cleaned = re.sub(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", cleaned)
    # A page reader returns each separator as a word of its own, so a printed "07/30/2026"
    # arrives as "07 / 30 / 2026". The spacing is the reader's, not the document's.
    cleaned = re.sub(r"\s*([-/.])\s*", r"\1", cleaned)
    # An alphabetic month fixes the day/month order, so a two-digit year is still
    # unambiguous: suppliers print "14-AUG-26" and that can only be 2026-08-14.
    month_name = re.fullmatch(r"(\d{1,2})[- /.]([A-Za-z]{3,9})[- /.](\d{2,4})", cleaned)
    if not month_name:
        month_first = re.fullmatch(r"([A-Za-z]{3,9})[- /.](\d{1,2})[- /.](\d{2,4})", cleaned)
        if month_first:
            return _build_date(
                month_first.group(3), month_first.group(1), month_first.group(2)
            )
    if month_name:
        return _build_date(month_name.group(3), month_name.group(2), month_name.group(1))
    iso = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", cleaned)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
        except ValueError:
            return None
    numeric = re.fullmatch(r"(\d{1,2})([-/.])(\d{1,2})\2(\d{4})", cleaned)
    if numeric:
        day, separator, month, year = numeric.groups()
        day, month, year = int(day), int(month), int(year)
        # Dots and hyphens are the day-first (UAE/KSA) forms; only a slash may be a US month-first date.
        if separator == "/" and day <= 12 < month:
            day, month = month, day
        elif separator == "/" and day <= 12 and day != month:
            return None
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _month_number(value: str) -> int:
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "sept": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    key = value.casefold()[:4] if value.casefold().startswith("sept") else value.casefold()[:3]
    if key not in months:
        raise ValueError("unknown month")
    return months[key]


def _extract_currency(lines: list[str]) -> str | None:
    values: list[str] = []
    patterns = (
        # An account's, a bank's or a base currency is not the invoice's.
        re.compile(r"(?i)(?<!account\s)(?<!base\s)(?<!bank\s)\bcurrency\s*:\s*([A-Z]{3})\b"),
        re.compile(rf"(?i)^\s*currency\s+({_KNOWN_CURRENCY})\b"),
        re.compile(r"(?i)\btotal\s+in\s+([A-Z]{3})\b"),
        re.compile(r"(?i)^\s*total\s+([A-Z]{3})\s+[0-9][0-9,.]*\s*$"),
        re.compile(r"(?i)\bamount\s+(AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR)\b"),
        # "Total Amount Due (Incl. VAT)  AED  21,604.80": the code stands in front of the figure.
        re.compile(
            r"(?i)\b(AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR|INR)\s+[0-9][0-9,]*\.[0-9]{2,3}\b"
        ),
        # "Invoice Total (AED) 4,916.97": the code is bracketed beside the label. Only a
        # known code is read, so "Net Total (Excl. VAT)" is not mistaken for a currency.
        re.compile(
            r"(?i)\b(?:total|amount|net|subtotal|sub\s*total|vat|tax|value)\b[^0-9()]{0,24}"
            r"\(\s*(AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR|INR)\s*\)"
        ),
        # "Inv.With VAT 4,916.97 AED": the code follows its figure.
        re.compile(r"(?i)[0-9]\.[0-9]{2,3}\s*(AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR|INR)\b"),
        # "Amount in words: ... ninety-nine AED and 96 Fils" / "... AED Only"
        re.compile(
            r"(?i)\b(AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR|INR)\b\s*"
            r"(?:and\s+[\w-]+\s+(?:fils|fil|baisa|baisas|halala|halalas|cents?)\b|only\b)"
        ),
    )
    for line in lines:
        if re.search(r"(?i)\b(?:equivalent|exchange\s+rate|rate\s+of\s+exchange|converted)\b", line):
            continue
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                candidate = match.group(1).upper()
                if candidate in ISO_CURRENCIES:
                    values.append(candidate)
                break
    return _only_distinct(values)


def _currency_under_label(rows: list[tuple[int, list[_Word]]]) -> str | None:
    """A known code printed directly under a "Currency" column heading; two different ones leave it unread."""

    values: list[str] = []
    for index, (page, row) in enumerate(rows):
        for label in row:
            if _normal(label.text) != "currency":
                continue
            before = [word for word in row if word.x < label.x]
            if before and _normal(max(before, key=lambda word: word.x).text) in {"account", "base", "bank"}:
                continue
            for next_page, below in rows[index + 1 :]:
                if next_page != page or _row_y(below) - _row_y(row) > label.height * 4:
                    break
                codes = [
                    word.text for word in below
                    if word.x0 < label.x1 + label.height and word.x1 > label.x0 - label.height
                    and re.fullmatch(_KNOWN_CURRENCY, word.text)
                ]
                if codes:
                    values.extend(codes)
                    break
    return _only_distinct(values)


def _extract_money(lines: list[str], patterns: tuple[re.Pattern[str], ...]) -> Decimal | None:
    values: list[Decimal] = []
    for line in lines:
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            parsed = _parse_decimal(match.group(1))
            if parsed is not None:
                values.append(parsed)
            break
    distinct = {value for value in values}
    if len(distinct) != 1:
        return None
    return next(iter(distinct))


def _parse_decimal(value: str) -> Decimal | None:
    """A printed amount (app.numbers.to_decimal); a negative is never an amount the layout reads."""
    result = to_decimal(value)
    return result if result is not None and result >= 0 else None


# A reader can return a thousands separator as a word of its own, so "1 , 028.80" arrives as
# three words. Glued back together it is one printed amount; read apart, the last piece alone
# would be a wrong total that looks perfectly plausible.
_NUMBER_PIECE = re.compile(r"^(?:,|[0-9][0-9,.'’]*)$")
# A reader can also glue the money mark to the figure: "234.50AED", "AED234.50", "RO30,467.184".
_GLUED_CODE = re.compile(rf"^(?:{_MONEY_MARK}|Rs)?\.?([0-9][0-9,.'’]*)(?:{_MONEY_MARK})?$")
# A group of thousands after a space: "676 507,80". Only the gap of one printed space joins them, so two columns of
# figures stay two figures, and only the piece before it can have opened a number.
_SPACE_GROUP = re.compile(r"^[0-9]{3}(?:[.,][0-9]+)?$")
_SPACE_HEAD = re.compile(r"^[0-9]{1,3}(?: [0-9]{3})*$")


def _same_figure(joined: str, previous: _Word, text: str, word: _Word) -> bool:
    """Whether this piece carries on the figure before it, split at a separator or at a space between thousands."""

    if _NUMBER_PIECE.match(joined.replace(" ", "")) and _NUMBER_PIECE.match(text) and (
        joined.endswith(",") or text.startswith(",")
    ):
        return True
    return bool(_SPACE_HEAD.match(joined) and _SPACE_GROUP.match(text) and word.x0 - previous.x1 <= word.height * 0.8)
# "Total amount (excl. VAT)" is the printed net. The same words with "incl." are the gross,
# and OCR drops a letter often enough that the abbreviation is matched loosely. The label has
# to open the row: the same words inside a column heading are not a total.
_NET_TOTAL_LABEL = re.compile(
    r"(?i)\btotal(?:\s+\S+){0,2}\s*\(?\s*(?:excl|excluding|exclusive|exc|exl|ex)\b"
    r"[\s.]*(?:of\s+)?(?:vat|tax)\b"
)


def _row_numbers(row: list[_Word]) -> list[Decimal]:
    """The row's printed amounts, with a separated thousands separator read as one number."""

    joined: list[str] = []
    previous = None
    for word in sorted(row, key=lambda item: item.x):
        text = word.text.strip()
        if glued := _GLUED_CODE.match(text):
            text = glued[1]
        if joined and previous is not None and _same_figure(joined[-1], previous, text, word):
            joined[-1] += (" " if _SPACE_GROUP.match(text) and not joined[-1].endswith(",") else "") + text
        else:
            joined.append(text)
        previous = word
    return [value for text in joined if (value := _parse_decimal(text)) is not None]


_LABEL_FILLER = re.compile(rf"(?i)amount|amt|value|in|{_MONEY_MARK}|[.:=|_*()-]+|[^\x00-\x7f]+")
_NEXT_AMOUNT_LABEL = re.compile(r"(?:vat|tax|gst|total|grand|discount|less|add|shipping|freight|net|sub|subtotal|round)")


def _is_figure(word: _Word) -> bool:
    text = word.text.strip()
    return bool(_GLUED_CODE.match(text) or _NUMBER_PIECE.match(text))


def _figure_run(words: list[_Word]) -> list[Decimal]:
    """The first run of figures in the words, in reading order.

    A run that is a date or a count is no figures, and neither is one that can read as thousands spaced apart:
    "1 593 361,82" is one amount, whose last piece alone looks perfectly plausible, and "10 500.00" may be one too.
    A whole number under a two-letter mark is no figure either: "SR47362" and "RO 2026" are references.
    """

    run: list[_Word] = []
    ordered = sorted(words, key=lambda item: item.x)
    for word in ordered:
        text = word.text.strip()
        if _is_figure(word):
            run.append(word)
        elif run:
            if "/" in text or text == "-" or re.fullmatch(_COUNT_WORDS, _normal(text)):
                return []
            break
    pieces = [word.text.strip() for word in run]
    before = ordered[ordered.index(run[0]) - 1].text.strip() if run and ordered[0] is not run[0] else ""
    if not any("." in piece for piece in pieces) and (
        re.fullmatch(_SHORT_MARK, before) or any(re.search(_SHORT_MARK, piece) for piece in pieces)
    ):
        return []
    if len(pieces) > 1 and re.fullmatch(r"[0-9]{1,3}", pieces[0]) and all(
        re.fullmatch(r"[0-9]{3}(?:[.,][0-9]+)?", piece) for piece in pieces[1:]
    ):
        return []
    return _row_numbers(run)


def _labelled_net(row: list[_Word]) -> Decimal | None:
    """The amount right after a net label in the row, never a figure of the labels around it.

    The label can stand late in a row ("Payment Communication: ... Untaxed Amount 1,234.50"), and another label can
    follow its amount ("Taxable Amount 100.00 VAT 5.00", "Net Amount 4,500 VAT 225"). Of several figures the last with
    decimals is the amount ("Sub Total 10 1,500.00"); whole numbers in a row can as well be a date ("12 03 2099").
    Only money wording or a bracket stands between the label and its figure ("Sub Total (10 items) 1,500.00"). Anything
    else makes it another figure's ("Net Amount VAT 225", "Subtotal Ref 47362"), and so does more figures after it
    that no label claims ("Subtotal Qty: 12 Amount 4,500.00", "Sub Total Page 1 of 2 4,500.00").
    """

    ordered = sorted(row, key=lambda item: item.x)
    for end in range(1, len(ordered) + 1):
        normal = _normal(_row_text(ordered[:end]))
        if (
            re.search(_NET_WORDING, normal)
            or re.search(r"\b(?:sub\s*total|subtotal|net amount)\b", normal)
            or _NET_TOTAL_LABEL.match(normal)
        ):
            break
    else:
        return None
    first = next((index for index in range(end, len(ordered)) if _is_figure(ordered[index])), None)
    if first is None:
        return None
    between = re.sub(r"\([^)]*\)", " ", _TAX_ONLY_NAMED.sub(" ", _row_text(ordered[end:first])))
    if not all(_LABEL_FILLER.fullmatch(word.rstrip(".:=") or ":") for word in between.split()):
        return None
    stop = next((index for index in range(first, len(ordered)) if not _is_figure(ordered[index])), len(ordered))
    after = ordered[stop:]
    while after and re.fullmatch(_MONEY_MARK, after[0].text.strip()):
        after = after[1:]
    if any(_is_figure(word) for word in after) and not _NEXT_AMOUNT_LABEL.match(_normal(after[0].text)):
        return None
    numbers = _figure_run(ordered[end:])
    if len(numbers) > 1 and numbers[-1].as_tuple().exponent == 0:
        return None
    return numbers[-1] if numbers else None


def _tax_row_amount(row: list[_Word]) -> Decimal | None:
    """A tax row's amount: its last figure once the rate is set aside ("VAT 5 % 225.00", "VAT @ 5.00 225.00").

    A rate alone is none, and the figures stop at the next label: "VAT 225.00 Total 4,725.00" has no tax of 4,725.
    """

    ordered = sorted(row, key=lambda item: item.x)
    kept = [
        word for index, word in enumerate(ordered)
        if "%" not in word.text and "@" not in word.text
        and not (index + 1 < len(ordered) and ordered[index + 1].text.strip().startswith("%"))
        and not (index > 0 and ordered[index - 1].text.strip() == "@")
    ]
    numbers = _figure_run(kept)
    if not numbers:
        return None
    # "VAT 5" is a rate printed without its sign, not five of anything.
    if len(numbers) == 1 and numbers[0] <= 100 and numbers[0].as_tuple().exponent == 0:
        return None
    return numbers[-1]


def _bare_figure(text: str) -> str:
    glued = _GLUED_CODE.match(text)
    return glued[1] if glued else text


def _figure_text(group: list[_Word]) -> str:
    return " ".join(_bare_figure(word.text.strip()) for word in group).replace(" ,", ",").replace(", ", ",")


def _summary_figures(row: list[_Word]) -> list[tuple[Decimal, float, float]]:
    """The row's figures with the span each one covers, a figure in pieces ("1 , 496.50") counted once."""

    groups: list[list[_Word]] = []
    for word in sorted(row, key=lambda item: item.x):
        text = word.text.strip()
        if not _GLUED_CODE.match(text) and not _NUMBER_PIECE.match(text):
            continue
        if groups and _same_figure(_figure_text(groups[-1]), groups[-1][-1], _bare_figure(text), word):
            groups[-1].append(word)
        else:
            groups.append([word])
    return [(numbers[0], group[0].x0, group[-1].x1) for group in groups if len(numbers := _row_numbers(group)) == 1]


def _summary_columns(row: list[_Word]) -> list[tuple[float, float]] | None:
    """The spans of the Taxable, tax amount and Total columns of a heading row, in that order, or None.

    A tax word followed by a rate, number, code or date heads no amount: "VAT %", "Tax Rate", "VAT No", "Tax Date".
    """

    ordered = sorted(row, key=lambda item: item.x)
    names = [_normal(word.text) for word in ordered]

    def span(index: int) -> tuple[float, float]:
        amount = index + 1 < len(names) and names[index + 1] in {"amount", "amt", "value"}
        return ordered[index].x0, ordered[index + (1 if amount else 0)].x1

    def counts(index: int) -> bool:
        return index + 1 < len(names) and re.fullmatch(rf"{_COUNT_WORDS}|count", names[index + 1]) is not None

    def is_taxable(index: int) -> bool:
        return (
            re.fullmatch(r"taxable(?: amount| value)?", names[index]) is not None
            and (index == 0 or names[index - 1] not in {"non", "exempt"})
            and not counts(index)
        )

    def is_tax(index: int) -> bool:
        following = names[index + 1] if index + 1 < len(names) else ""
        return (
            re.fullmatch(r"(?:vat|tax|gst)(?: amount| amt)?", names[index]) is not None
            and "%" not in ordered[index].text
            and not (index + 1 < len(ordered) and "%" in ordered[index + 1].text)
            and following not in {"rate", "no", "number", "code", "id", "date", "percent", "pct", "type", "reg"}
            and not counts(index)
        )

    found: list[tuple[float, float]] = []
    start = 0
    for heading in (is_taxable, is_tax, lambda index: names[index].startswith("total") and not counts(index)):
        index = next((index for index in range(start, len(names)) if heading(index)), None)
        if index is None:
            return None
        found.append(span(index))
        start = index + 1
    return found


def _money_on_later_page(row: list[_Word]) -> bool:
    """A later page's row may hold the real summary. Two or three decimals are money, and so are whole figures only
    where they add up: a spaced phone or date, or "Form version 1.2", holds no summary."""

    figures = [value for value, _, _ in _summary_figures(row)]
    return any(value.as_tuple().exponent in (-2, -3) for value in figures) or (
        len(figures) >= 3 and figures[-3] + figures[-2] == figures[-1]
    )


# A table of these sums up some other document or period than the one in hand.
_ANOTHER_DOCUMENT = re.compile(r"(?i)\b(?:ytd|year\s+to\s+date|previous|prior|last|cumulative|brought\s+forward|credit\s+(?:memo|note))\b")
_NEGATED_ABOVE = re.compile(r"(?i)^\W*(?:non|not|exempt|zero|غير|معف\w*)\W*$")


def _heading_of_another_kind(heading: list[_Word], above: list[_Word]) -> bool:
    """The heading row says so itself, or the words printed directly over its columns do: a two-line "Non / Taxable"."""

    if _ANOTHER_DOCUMENT.search(_row_text(heading)):
        return True
    if not above or above[0].page != heading[0].page or _row_y(heading) - _row_y(above) > 3.5 * max(w.height for w in heading):
        return False
    over = sorted((w for w in above if any(w.x0 < h.x1 and w.x1 > h.x0 for h in heading)), key=lambda w: w.x)
    return bool(over) and bool(_ANOTHER_DOCUMENT.search(_row_text(above)) or _NEGATED_ABOVE.match(_row_text(over)))


def _summary_table_totals(rows: list[list[_Word]]) -> tuple[Decimal | None, Decimal | None]:
    """Net and tax under a "Rate | Taxable | VAT | Total" heading, whose figures' row prints no label of its own.

    The rows are the whole document's, in page order, since a table can run on over a page. A row of figures counts
    only with one figure under each heading and taxable + tax == total. One such row is the summary. Several are line
    rows or several rates, and one rate's figures are not the invoice's: only a last row printed as the Total, and
    equal to the sum of those above it, is read then. A heading that failed is not tried again where it repeats.
    A later page that prints amounts anywhere else, however far sideways, may hold the real summary: nothing is read.
    """

    def same(first: list[tuple[float, float]], second: list[tuple[float, float]]) -> bool:
        return all(a[0] < b[1] and a[1] > b[0] for a, b in zip(first, second))

    refused: list[list[tuple[float, float]]] = []
    for index, row in enumerate(rows):
        columns = _summary_columns(row)
        if columns is None or any(same(columns, earlier) for earlier in refused):
            continue
        if _heading_of_another_kind(row, rows[index - 1] if index else []):
            continue
        found: list[tuple[list[Decimal], list[_Word]]] = []
        for below in rows[index + 1 :]:
            figures = _summary_figures(below)
            cells = [[value for value, left, right in figures if left < x1 and right > x0] for x0, x1 in columns]
            if not cells[0] and not cells[1]:
                continue
            if any(len(cell) != 1 for cell in cells) or cells[0][0] + cells[1][0] != cells[2][0]:
                found = []
                break
            found.append(([cell[0] for cell in cells], below))
        if len(found) > 1:
            (last, last_row), above = found[-1], [values for values, _ in found[:-1]]
            printed_total = re.search(r"\btotal\b", _normal(_row_text(last_row)))
            if not printed_total or any(last[n] != sum(values[n] for values in above) for n in range(3)):
                found = []
            else:
                found = found[-1:]
        if found:
            page = found[0][1][0].page
            if any(later[0].page > page and (_money_on_later_page(later) or _summary_columns(later)) for later in rows):
                return None, None
            return found[0][0][0], found[0][0][1]
        refused.append(columns)
    return None, None


def _net_stands_as_label(row: list[_Word], normal: str) -> bool:
    """ "Last invoice Sub Total: 913.50" in a sentence is about something else."""

    from app.header_labels import row_labels, tokens

    if not re.search(r"\b(?:sub\s*total|subtotal|net amount)\b", normal) or _NET_TOTAL_LABEL.match(normal):
        return True
    return any(label.field == "net" for label in row_labels(tokens(row)))


def _extract_spatial_totals(words: list[_Word]) -> tuple[Decimal | None, Decimal | None]:
    """Read printed invoice net and tax from an explicit currency total row."""

    nets: list[Decimal] = []
    taxes: list[Decimal] = []
    document_rows: list[list[_Word]] = []
    for page in sorted({word.page for word in words}):
        page_words = [word for word in words if word.page == page]
        rows = _group_page_rows(page_words)
        if not rows:
            continue
        document_rows += rows
        for row in rows:
            normal = _normal(_row_text(row))
            if _net_stands_as_label(row, normal) and (labelled := _labelled_net(row)) is not None:
                nets.append(labelled)
            # A scan's reader returns the label as one word: "VATAMOUNT @5% 1,028.80". "VAT5%" is a label too.
            if _TAX_AMOUNT_LABEL.match(normal) and (amount := _tax_row_amount(row)) is not None:
                taxes.append(amount)
        typical_height = median(word.height for word in page_words)
        index = 0
        while index < len(rows):
            columns, header_end = _find_header(rows, index, typical_height)
            if columns is None:
                break
            for row in rows[header_end + 1 :]:
                normal = _normal(_row_text(row))
                if not re.search(r"\btotal\s+in\s+[a-z]{3}\b", normal):
                    continue
                net = _decimal_in_bounds(row, columns.net_bounds)
                tax = _decimal_in_bounds(row, columns.tax_bounds)
                if net is not None:
                    nets.append(net)
                if tax is not None:
                    taxes.append(tax)
                break
            index = header_end + 1
    summary_net, summary_tax = _summary_table_totals(document_rows)
    if summary_net is not None:
        nets.append(summary_net)
        taxes.append(summary_tax)

    def only(values: list[Decimal]) -> Decimal | None:
        distinct = set(values)
        return next(iter(distinct)) if len(distinct) == 1 else None

    return only(nets), only(taxes)


# A line printed with no column heading: an optional item code, a barcode, the description,
# then the quantity, its unit and the unit price ("LUM-SER30 00012345678905 Lumena serum 30 ml
# 10 EA 60.00").
_BARCODE_ROW = re.compile(
    r"^(?:(?P<sku>[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\s+)?(?P<gtin>[0-9]{8}|[0-9]{12,14})\s+"
    r"(?P<description>\S.*?)\s+(?P<qty>[0-9]+(?:\.[0-9]+)?)\s+(?P<uom>[A-Z]{1,4})\s+"
    r"(?P<price>[0-9][0-9,]*\.[0-9]{2,3})$"
)


def _barcode_rows(lines: list[str], net: Decimal) -> list[Line]:
    """The headerless barcode rows of a page, kept only when they sum exactly to the printed net.

    With no heading to say which column is which, the row shape alone is weak evidence: every
    barcode has to pass its check digit, and the quantities times the prices have to come to the
    net printed on the same document, so a stray row read as a line never stands.
    """

    from app.scan_guard import gtin_valid

    found: list[Line] = []
    for text in lines:
        match = _BARCODE_ROW.match(text)
        if match is None or not gtin_valid(match["gtin"]):
            continue
        price = _parse_decimal(match["price"])
        description = _clean_description(match["description"])
        if price is None or description is None:
            return []
        found.append(Line(sku=match["sku"], gtin=match["gtin"], description=description,
                          qty=Decimal(match["qty"]), uom=match["uom"], price=price,
                          evidence=text[:500]))
    if not found or sum(line.qty * line.price for line in found) != net:
        return []
    return found


def _extract_table_lines(words: list[_Word]) -> list[Line]:
    result: list[Line] = []
    for page in sorted({word.page for word in words}):
        page_words = [word for word in words if word.page == page]
        rows = _group_page_rows(page_words)
        if not rows:
            continue
        typical_height = median(word.height for word in page_words)
        index = 0
        while index < len(rows):
            columns, header_end = _find_header(rows, index, typical_height)
            if columns is None:
                break
            index = header_end + 1
            draft: _DraftLine | None = None
            pending: list[list[_Word]] = []
            while index < len(rows):
                row = rows[index]
                row_text = _row_text(row)
                normal = _normal(row_text)
                if _is_totals_or_footer(normal):
                    break
                next_columns, _ = _find_header(rows, index, typical_height)
                if next_columns is not None:
                    break
                parts = _row_parts(row, columns)
                qty = _first_decimal(parts["qty"])
                price = _first_decimal(parts["price"])
                sku = _select_sku(parts["item"], columns.item)
                description = _clean_description_words(parts["description"])
                gtin = _extract_gtin_words(parts["description"])
                if (
                    qty is not None
                    and price is not None
                    and (sku is not None or gtin is not None or description is not None)
                ):
                    if draft is not None:
                        result.append(_finish_line(draft))
                    draft = _DraftLine(
                        sku=sku,
                        description=description,
                        uom=_clean_uom(_join_words(parts["uom"])),
                        qty=qty,
                        price=None if _dropped_decimal_point(qty, price, row, columns) else price,
                        evidence_parts=[row_text],
                        page=page,
                        last_y=_row_y(row),
                        gtin=gtin,
                        net_amount=_decimal_in_bounds(row, columns.net_bounds),
                        tax_amount=_decimal_in_bounds(row, columns.tax_bounds),
                    )
                    for prior in pending:
                        _merge_continuation(draft, prior, columns, prepend=True)
                    pending.clear()
                elif draft is not None and _is_near_row(draft, row, typical_height):
                    _merge_continuation(draft, row, columns, prepend=False)
                elif _has_continuation_content(parts):
                    pending.append(row)
                    if len(pending) > 2:
                        pending.pop(0)
                index += 1
            if draft is not None:
                result.append(_finish_line(draft))
            if index == header_end + 1:
                index += 1
    if len(result) > 1000:
        raise ValueError("Invoice contains more than the supported 1,000 line items")
    return result


def _find_header(
    rows: list[list[_Word]], start: int, typical_height: float
) -> tuple[_Columns | None, int]:
    for index in range(start, len(rows)):
        anchors = _header_anchors(rows[index])
        end = index
        if not {"description", "qty", "price"}.issubset(anchors) and index + 1 < len(rows):
            gap = abs(_row_y(rows[index + 1]) - _row_y(rows[index]))
            if gap <= max(8.0, typical_height * 2.0):
                combined = dict(anchors)
                combined.update(
                    {key: value for key, value in _header_anchors(rows[index + 1]).items() if key not in combined}
                )
                if len(combined) > len(anchors):
                    anchors = combined
                    end = index + 1
        if all(key in anchors for key in ("description", "qty", "price")):
            item = anchors.get("item")
            description = anchors["description"]
            uom = anchors.get("uom")
            qty = anchors["qty"]
            price = anchors["price"]
            serial = anchors.get("serial")
            identity_columns = [value for value in (serial, item, description, uom) if value is not None]
            if (
                not description < qty < price
                or any(value >= qty for value in identity_columns)
                or len(set((*identity_columns, qty, price))) != len(identity_columns) + 2
            ):
                continue
            later = sorted(
                word.x
                for word in rows[index] + (rows[end] if end != index else [])
                if word.x > price + 4 and _normal(word.text) in _AFTER_PRICE_HEADERS
            )
            page_width = rows[index][0].page_width
            price_right = (price + later[0]) / 2 if later else page_width
            net_bounds, tax_bounds = _financial_column_bounds(
                rows, index, end, price, typical_height
            )
            columns = _Columns(
                item=item,
                description=description,
                uom=uom,
                qty=qty,
                price=price,
                price_right=price_right,
                serial=serial,
                net_bounds=net_bounds,
                tax_bounds=tax_bounds,
            )
            columns = replace(columns, splits=_column_splits(rows, end + 1, columns))
            if net_bounds is None or tax_bounds is None:
                columns = _arithmetic_money_columns(rows, end + 1, columns, typical_height)
            return columns, end
    return None, start


def _arithmetic_money_columns(
    rows: list[list[_Word]], start: int, columns: _Columns, typical_height: float
) -> _Columns:
    """Name the line amount columns from the figures when the headings do not name them.

    A photographed heading reads as "VATAMOUNT", or as "Total Amount" over two printed lines,
    and is not recognised; the figures below it still say which column is which. The line net
    is the column holding quantity x price, and the line tax is the column that is the same
    fraction of that net on every row. Nothing is taken unless several rows agree.
    """

    if columns.net_bounds is not None and columns.tax_bounds is not None:
        return columns
    data: list[list[_Word]] = []
    for row in rows[start : start + 60]:
        if data and _is_totals_or_footer(_normal(_row_text(row))):
            break
        parts = _row_parts(row, columns)
        qty = _first_decimal(parts["qty"])
        price = _first_decimal(parts["price"])
        if qty is None or price is None or not qty or not price:
            continue
        data.append(row)
    if len(data) < 3:
        return columns

    gap = max(4.0, typical_height * 0.8)
    spans: list[list[float]] = []
    for word in sorted(
        (word for row in data for word in row if word.x > columns.price_right),
        key=lambda word: word.x0,
    ):
        if spans and word.x0 - spans[-1][1] <= gap:
            spans[-1][1] = max(spans[-1][1], word.x1)
        else:
            spans.append([word.x0, word.x1])
    bounds = [(left, right) for left, right in spans if right > left]
    if not bounds:
        return columns

    expected = [
        _first_decimal(_row_parts(row, columns)["qty"]) * _first_decimal(_row_parts(row, columns)["price"])
        for row in data
    ]
    read = [[_decimal_in_bounds(row, bound) for bound in bounds] for row in data]

    net_column = None
    if columns.net_bounds is None:
        agreement = []
        for position, _bound in enumerate(bounds):
            agree = sum(
                1
                for target, values in zip(expected, read)
                if values[position] is not None
                and abs(values[position] - target) <= max(Decimal("0.05"), target / 1000)
            )
            agreement.append((agree, position))
        agree, net_column = max(agreement)
        if agree < max(3, round(len(data) * 0.7)):
            return columns

    tax_column = None
    if columns.tax_bounds is None and net_column is not None:
        candidates: list[tuple[Decimal, int]] = []
        for position, _bound in enumerate(bounds):
            if position == net_column:
                continue
            ratios = [
                values[position] / values[net_column]
                for values in read
                if values[position] is not None and values[net_column]
            ]
            if len(ratios) < 3:
                continue
            rate = median(ratios)
            if not Decimal(0) < rate < Decimal("0.5"):
                continue
            spread = max(abs(ratio - rate) for ratio in ratios)
            # A tax column is the one rate on every row; rounding to the printed
            # minor unit is the only difference allowed between them.
            if spread <= rate / 50:
                candidates.append((spread, position))
        if candidates:
            tax_column = min(candidates)[1]

    def widened(position: int | None) -> tuple[float, float] | None:
        if position is None:
            return None
        left, right = bounds[position]
        before = bounds[position - 1][1] if position else columns.price_right
        after = bounds[position + 1][0] if position + 1 < len(bounds) else rows[start][0].page_width
        return (left + before) / 2, (right + after) / 2

    return replace(
        columns,
        net_bounds=columns.net_bounds or widened(net_column),
        tax_bounds=columns.tax_bounds or widened(tax_column),
    )


_AFTER_PRICE_HEADERS = {
    "amount",
    "amt",
    "disc",
    "discount",
    "gross",
    # A margin or markup column is printed between the unit price and the amounts.
    "margin",
    "markup",
    "net",
    "rate",
    "subtotal",
    "tax",
    "total",
    "value",
    "vat",
}


def _header_anchors(row: list[_Word]) -> dict[str, float]:
    anchors: dict[str, float] = {}
    repeated_spanned_headers: list[_Word] = []
    for word in row:
        value = _normal(word.text)
        compact = re.sub(r"[^a-z0-9]", "", word.text.casefold())
        if all(part in compact for part in ("itemcode", "description", "uom", "qty")):
            repeated_spanned_headers.append(word)
        serial_part = next(
            (part for part in ("serialnumber", "serialno", "srno", "sno") if part in compact),
            None,
        )
        item_part = next(
            (part for part in ("itemnumber", "itemcode", "itemno", "barcode") if part in compact), None
        )
        description_part = next(
            (
                part
                for part in (
                    "productdescription",
                    "itemdescription",
                    "servicedescription",
                    "description",
                    "productname",
                    "itemname",
                    "particulars",
                    "desc",
                )
                if part in compact
            ),
            None,
        )
        uom_part = next(
            (part for part in ("unitofmeasure", "measureunit", "uom") if part in compact),
            None,
        )
        qty_part = next(
            (part for part in ("quantity", "qty") if part in compact),
            None,
        )
        price_part = next(
            (part for part in ("sellingprice", "unitprice", "priceperunit", "price", "rate") if part in compact),
            None,
        )
        if serial_part:
            anchors.setdefault("serial", _substring_x(word, compact, serial_part))
        elif value in {"s no", "sno", "sn", "sn no", "sr", "sr no", "serial", "serial no",
                       "line no", "line number"}:
            anchors.setdefault("serial", word.x)
        if item_part:
            anchors.setdefault("item", _substring_x(word, compact, item_part))
        elif re.fullmatch(r"item\s*(?:no|number|code)?|sku|product\s*code|bar\s*code|ean|gtin|upc", value):
            anchors.setdefault("item", word.x)
        if description_part:
            anchors.setdefault("description", _substring_x(word, compact, description_part))
        elif value.startswith("desc") or value in {
            "name",
            "item name",
            "product name",
            "service description",
            "item description",
            "product description",
            "particulars",
        }:
            anchors.setdefault("description", word.x)
        if uom_part:
            anchors.setdefault("uom", _substring_x(word, compact, uom_part))
        elif value in {"uom", "u m", "unit", "unit of measure"}:
            anchors.setdefault("uom", word.x)
        if qty_part:
            anchors.setdefault("qty", _substring_x(word, compact, qty_part))
        elif value in {"qty", "quantity"}:
            anchors.setdefault("qty", word.x)
        if price_part:
            anchors.setdefault("price", _substring_x(word, compact, price_part))
        elif value in {"price", "unit price", "unitprice", "rate"}:
            anchors.setdefault("price", word.x)

    if len(repeated_spanned_headers) >= 3:
        # Some Docling scan tables repeat one merged header string in each
        # covered physical cell. The measured cell centres still identify the
        # UOM and quantity columns; the first wide cell retains item/description.
        repeated_spanned_headers.sort(key=lambda word: word.x)
        anchors["uom"] = repeated_spanned_headers[1].x
        anchors["qty"] = repeated_spanned_headers[2].x

    ordered = sorted(row, key=lambda word: word.x)
    phrases = {
        "product name": "description",
        "item name": "description",
        "product description": "description",
        "item description": "description",
        "service description": "description",
        "unit price": "price",
        "s no": "serial",
        "sr no": "serial",
        "line no": "serial",
        "line number": "serial",
    }
    for first, second in zip(ordered, ordered[1:]):
        phrase = f"{_normal(first.text)} {_normal(second.text)}"
        role = phrases.get(phrase)
        if role is None:
            continue
        centre = (first.x + second.x) / 2
        anchors.setdefault(role, centre)
        if role == "price" and anchors.get("uom") == first.x:
            anchors.pop("uom")

    # "Unit" is a column of its own only when it does not open a price label. A scan reads
    # "Unit (excl. VAT) Price" with the bracketed note between the two words, so the pair
    # above never forms: a bare "Unit" with the price to its right and no other column
    # header in between belongs to that price.
    bare_unit = next((word.x for word in row if _normal(word.text) == "unit"), None)
    price_x = anchors.get("price")
    if bare_unit is not None and price_x is not None and anchors.get("uom") == bare_unit < price_x:
        if not [
            value
            for key, value in anchors.items()
            if key not in ("uom", "price") and bare_unit < value < price_x
        ]:
            anchors.pop("uom")
    return anchors


def _substring_x(word: _Word, compact: str, part: str) -> float:
    start = compact.find(part)
    if start < 0 or not compact:
        return word.x
    centre = start + len(part) / 2
    return word.x0 + (word.x1 - word.x0) * centre / len(compact)


def _heading_phrases(
    main_row: list[_Word], consumed: set[int], price: float
) -> list[list[_Word]]:
    """The heading labels printed to the right of the unit price, one phrase per column.

    "Net Value" is two words of one label and "VAT % VAT Amount" is two labels of two words
    each; the space inside a label is far narrower than the space between columns, so the
    words group by the gaps between them.
    """

    heading = [
        word
        for position, word in enumerate(main_row)
        if position not in consumed and word.x > price + 4
    ]
    if not heading:
        return []
    gap = max(6.0, median(word.height for word in heading) * 0.8)
    groups: list[list[_Word]] = []
    for word in sorted(heading, key=lambda item: item.x0):
        if groups and word.x0 - max(prior.x1 for prior in groups[-1]) <= gap:
            groups[-1].append(word)
        else:
            groups.append([word])
    return groups


def _financial_label_kind(group: list[_Word], upper_words: list[_Word]) -> str | None:
    """What a heading right of the unit price names: the line net, the line tax, or neither.

    The phrase decides, not the single word: "Net Value" and "VAT Amount" each name a money
    column, while the "VAT %" printed between them names a rate and must never be read as a
    tax amount. A phrase holding no heading word at all is not a column label.
    """

    text = " ".join(word.text for word in group)
    phrase = _normal(text)
    if not any(token in _AFTER_PRICE_HEADERS for token in phrase.split()):
        return None
    if "%" in text or re.search(r"\b(?:rate|percent|pct)\b", phrase):
        return "other"
    if re.search(r"\b(?:net|taxable|assessable)\b", phrase):
        return "net"
    if re.search(r"\b(?:vat|tax|gst)\b", phrase) and re.search(
        r"\b(?:amount|amt|value)\b", phrase
    ):
        return "tax"
    if phrase in {"amt", "amount"} and any(
        _normal(upper.text) in {"vat", "tax"}
        and abs(upper.x - group[0].x) <= max(30.0, group[0].height * 2.5)
        for upper in upper_words
    ):
        return "tax"
    return "other"


def _financial_column_bounds(
    rows: list[list[_Word]], index: int, end: int, price: float, typical_height: float
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    header_rows = list(rows[index : end + 1])
    if index > 0 and abs(_row_y(rows[index]) - _row_y(rows[index - 1])) <= max(
        12.0, typical_height * 2.0
    ):
        header_rows.insert(0, rows[index - 1])

    main_row = rows[end]
    labels: list[tuple[str, float]] = []
    repeated_financial = sorted(
        (
            word for word in main_row
            if all(
                part in re.sub(r"[^a-z0-9]", "", word.text.casefold())
                for part in ("value", "disc", "subtotal")
            )
        ),
        key=lambda word: word.x,
    )
    if len(repeated_financial) >= 3:
        labels.extend(("other", word.x) for word in repeated_financial[:2])
        labels.append(("net", repeated_financial[2].x))
        labels.extend(("other", word.x) for word in repeated_financial[3:])
    repeated_vat_total = sorted(
        (
            word for word in main_row
            if all(
                part in re.sub(r"[^a-z0-9]", "", word.text.casefold())
                for part in ("amt", "total")
            )
        ),
        key=lambda word: word.x,
    )
    if len(repeated_vat_total) >= 2:
        labels.append(("tax", repeated_vat_total[0].x))
        labels.extend(("other", word.x) for word in repeated_vat_total[1:])
    consumed: set[int] = set()
    for position, word in enumerate(main_row):
        value = _normal(word.text)
        following = _normal(main_row[position + 1].text) if position + 1 < len(main_row) else ""
        if value == "sub" and following == "total":
            labels.append(("net", (word.x + main_row[position + 1].x) / 2))
            consumed.update((position, position + 1))
        elif value in {"subtotal", "sub total", "net amount", "taxable value"}:
            labels.append(("net", word.x))
            consumed.add(position)

    upper_words = [word for row in header_rows[:-1] for word in row]
    for group in _heading_phrases(main_row, consumed, price):
        kind = _financial_label_kind(group, upper_words)
        if kind is None:
            continue
        centre = (min(word.x0 for word in group) + max(word.x1 for word in group)) / 2
        labels.append((kind, centre))

    # Preserve the unit-price boundary even when its label is split across rows.
    centres = sorted({price, *(centre for _, centre in labels)})
    if len(centres) < 2:
        return None, None

    def bounds(kind: str) -> tuple[float, float] | None:
        targets = [centre for label, centre in labels if label == kind]
        if len(targets) != 1:
            return None
        target = targets[0]
        position = centres.index(target)
        left = (centres[position - 1] + target) / 2 if position else 0.0
        right = (
            (target + centres[position + 1]) / 2
            if position + 1 < len(centres)
            else main_row[0].page_width
        )
        return left, right

    return bounds("net"), bounds("tax")


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _label_phrase(words: list[_Word]) -> str:
    """A label as printed, keeping a '#' sign: "INVOICE #" and "INVOICE#" read alike."""

    spelled = " ".join(re.sub(r"[^a-z0-9#]+", " ", word.text.casefold()) for word in words)
    return re.sub(r"\s*#\s*", "#", " ".join(spelled.split())).strip()


def _row_y(row: list[_Word]) -> float:
    return sum(word.y for word in row) / len(row)


def _column_anchors(columns: _Columns) -> list[tuple[str, float]]:
    """The named columns of a table heading, left to right."""

    anchors: list[tuple[str, float]] = []
    if columns.serial is not None:
        anchors.append(("serial", columns.serial))
    if columns.item is not None:
        anchors.append(("item", columns.item))
    anchors.append(("description", columns.description))
    if columns.uom is not None:
        anchors.append(("uom", columns.uom))
    anchors.extend((("qty", columns.qty), ("price", columns.price)))
    anchors.sort(key=lambda anchor: anchor[1])
    return anchors


def _anchor_boundaries(anchors: list[tuple[str, float]], columns: _Columns) -> list[float]:
    boundaries: list[float] = []
    for (left_role, left), (right_role, right) in zip(anchors, anchors[1:]):
        if left_role == "serial" and right_role == "item":
            # A serial column is normally narrow.  Keep its numeric row marker
            # out of the SKU while allowing a left-aligned item code beneath a
            # merged "S.No Item Code" heading.
            boundaries.append(left + (right - left) * 0.25)
        elif left_role == "description" and columns.uom is not None:
            boundaries.append(right - max(3.0, (right - left) * 0.08))
        else:
            boundaries.append((left + right) / 2)
    return boundaries


def _column_bands(words: list[_Word], gap: float) -> list[list[float]]:
    """The runs of print that have clear white space down both sides of them."""

    bands: list[list[float]] = []
    for word in sorted(words, key=lambda item: item.x0):
        if bands and word.x0 - bands[-1][1] <= gap:
            bands[-1][1] = max(bands[-1][1], word.x1)
        else:
            bands.append([word.x0, word.x1])
    return bands


def _band_of(bands: list[list[float]], anchor: float, reach: float) -> int | None:
    for position, (left, right) in enumerate(bands):
        if left <= anchor <= right:
            return position
    distances = sorted(
        (min(abs(anchor - left), abs(anchor - right)), position)
        for position, (left, right) in enumerate(bands)
    )
    if distances and distances[0][0] <= reach:
        return distances[0][1]
    return None


def _column_splits(
    rows: list[list[_Word]], start: int, columns: _Columns
) -> tuple[float, ...] | None:
    """Where one column stops and the next begins, learned from the rows under the heading.

    A heading label is printed left-aligned over a wide description and right-aligned over a
    narrow figure, so the midpoint between two labels can fall inside the text of the column
    on its left: "ACME ROSE EDP 50ML + CS50 + CASE" loses its last two words to the
    quantity column, which then reads "+ CASE 6" and holds no number at all, and the line is
    dropped for having no quantity. The rows under the heading show where each column really
    sits, because a column is a run of print with white space down both of its sides. The
    bands are used only when every named column lands in a band of its own in heading order,
    so a table whose columns touch keeps the midpoints.
    """

    anchors = _column_anchors(columns)
    body: list[list[_Word]] = []
    for row in rows[start : start + 60]:
        if body and _is_totals_or_footer(_normal(_row_text(row))):
            break
        body.append(row)
    words = [word for row in body for word in row if word.x < columns.price_right]
    if len(words) < len(anchors):
        return None
    height = median(word.height for word in words)
    for factor in (1.0, 1.4, 2.0):
        bands = _column_bands(words, max(5.0, height * factor))
        placed = [_band_of(bands, anchor, max(24.0, height * 3.0)) for _role, anchor in anchors]
        if None in placed or placed != sorted(set(placed)):
            continue
        return tuple(
            (bands[position - 1][1] + bands[position][0]) / 2 for position in placed[1:]
        )
    return None


def _row_parts(row: list[_Word], columns: _Columns) -> dict[str, list[_Word]]:
    parts = {"item": [], "description": [], "uom": [], "qty": [], "price": []}
    anchors = _column_anchors(columns)
    boundaries = (
        list(columns.splits)
        if columns.splits is not None and len(columns.splits) == len(anchors) - 1
        else _anchor_boundaries(anchors, columns)
    )
    for word in row:
        if word.x >= columns.price_right:
            continue
        column = len(anchors) - 1
        for position, boundary in enumerate(boundaries):
            if word.x < boundary:
                column = position
                break
        role = anchors[column][0]
        if role in parts:
            parts[role].append(word)
    return parts


def _join_words(words: list[_Word]) -> str:
    if not words:
        return ""
    tolerance = max(2.0, median(word.height for word in words) * 0.45)
    lines: list[list[_Word]] = []
    centres: list[float] = []
    for word in sorted(words, key=lambda item: (item.y, item.x)):
        if lines and abs(word.y - centres[-1]) <= tolerance:
            lines[-1].append(word)
            centres[-1] = sum(item.y for item in lines[-1]) / len(lines[-1])
        else:
            lines.append([word])
            centres.append(word.y)
    return " ".join(
        _printed_run(sorted(line, key=lambda item: item.x)) for line in lines
    ).strip()


def _printed_run(words: list[_Word]) -> str:
    """One run of words as printed.

    A reader that reports the text line a word came from spaces that line the way the page
    does, so "(US) AGE-R" is kept rather than respaced to "( US ) AGE-R". The line is used
    only when it covers exactly these words and nothing else.
    """

    spaced = " ".join(word.text for word in words)
    sources = {word.line for word in words}
    printed = next(iter(sources)) if len(sources) == 1 else None
    if printed and "".join(printed.split()) == "".join(spaced.split()):
        return printed
    return spaced


def _dropped_decimal_point(qty: Decimal, price: Decimal, row: list[_Word], columns: _Columns) -> bool:
    """A unit price OCR read without its decimal point: left empty rather than filled wrong.

    True only when the price was read as a whole number, quantity x price is not an amount printed right of
    the price column, and quantity x price / 10^k is one printed with exactly k decimals (e.g. 12 x "2400"
    beside a printed 28.800). Nothing is restored: the printed point itself was not read.
    """
    if price.as_tuple().exponent < 0 or not qty:
        return False
    amounts = [
        value for value in (_parse_decimal(word.text) for word in row if word.x >= columns.price_right)
        if value is not None and value
    ]
    if any(value == qty * price for value in amounts):
        return False
    return any(
        value.as_tuple().exponent == -places and value == qty * price.scaleb(-places)
        for value in amounts
        for places in (1, 2, 3)
    )


def _first_decimal(words: list[_Word]) -> Decimal | None:
    if any(_normal(word.text) in {"", "comma", "dot"} and not word.text.isalnum() for word in words):
        joined = "".join(word.text.strip() for word in sorted(words, key=lambda item: item.x))
        return _parse_decimal(joined)
    values = [_parse_decimal(word.text) for word in words]
    valid = [value for value in values if value is not None]
    if len(valid) != 1:
        return None
    return valid[0]


def _decimal_in_bounds(
    row: list[_Word], bounds: tuple[float, float] | None
) -> Decimal | None:
    if bounds is None:
        return None
    left, right = bounds
    return _first_decimal([word for word in row if left <= word.x < right])


def _column_cluster(words: list[_Word], anchor: float | None) -> list[_Word]:
    """The run of words printed in one column: the one the column's own header sits over.

    A table with no header over every column (a brand beside a barcode, a description that
    starts left of its heading) sweeps its neighbours into the nearest column, and a word
    from the next column over is not part of this one's value.
    """

    if anchor is None or len(words) < 2:
        return words
    ordered = sorted(words, key=lambda word: word.x0)
    gap = median(word.height for word in ordered) * 2.0
    clusters: list[list[_Word]] = [[ordered[0]]]
    for previous, word in zip(ordered, ordered[1:]):
        if word.x0 - previous.x1 > gap:
            clusters.append([])
        clusters[-1].append(word)
    return min(
        clusters,
        key=lambda cluster: min(abs(word.x - anchor) for word in cluster),
    )


def _select_sku(words: list[_Word], anchor: float | None = None) -> str | None:
    words = _column_cluster(words, anchor)
    candidates: list[tuple[int, int, str]] = []
    for word in words:
        value = word.text.strip().replace(" ", "")
        if not _SAFE_IDENTIFIER.fullmatch(value):
            continue
        normal = _normal(value)
        if normal in {"item", "no", "number", "s no", "serial"}:
            continue
        score = (3 if any(character.isalpha() for character in value) else 1) + (
            1 if any(character in "-_/" for character in value) else 0
        )
        candidates.append((score, len(value), value))
    if not candidates:
        return None
    alpha_parts = [
        word.text.strip().replace(" ", "")
        for word in sorted(words, key=lambda item: (item.y, item.x))
        if _SAFE_IDENTIFIER.fullmatch(word.text.strip().replace(" ", ""))
        and any(character.isalpha() for character in word.text)
    ]
    joined = "".join(alpha_parts)
    if len(alpha_parts) > 1 and _SAFE_IDENTIFIER.fullmatch(joined):
        return joined
    return max(candidates)[2]


def _clean_description(value: str) -> str | None:
    if not value:
        return None
    cleaned = _GTIN.sub("", value)
    cleaned = " ".join(cleaned.split()).strip(" -,:;")
    if not cleaned or _looks_dangerous(cleaned):
        return None
    return cleaned


def _extract_gtin(value: str) -> str | None:
    match = _GTIN.search(value)
    return match.group(1).replace(" ", "") if match else None


def _extract_gtin_words(words: list[_Word]) -> str | None:
    joined = _join_words(words)
    labeled = _extract_gtin(joined)
    if labeled:
        return labeled
    if not any(_normal(word.text) in {"ean", "gtin", "upc"} for word in words):
        return None
    values = {
        word.text.strip().replace(" ", "")
        for word in words
        if re.fullmatch(r"[0-9]{8,18}", word.text.strip().replace(" ", ""))
    }
    return next(iter(values)) if len(values) == 1 else None


def _clean_description_words(words: list[_Word]) -> str | None:
    gtin = _extract_gtin_words(words)
    kept: list[_Word] = []
    for word in words:
        value = word.text.strip()
        normal = _normal(value)
        compact = value.replace(" ", "")
        if normal in {"ean", "gtin", "upc"} or value in {":", "#"}:
            continue
        if gtin and compact == gtin:
            continue
        kept.append(word)
    return _clean_description(_join_words(kept))


def _clean_uom(value: str) -> str | None:
    cleaned = " ".join(value.split()).strip(" :")
    if not cleaned or len(cleaned) > 30 or _looks_dangerous(cleaned):
        return None
    return cleaned


def _looks_dangerous(value: str) -> bool:
    return value.lstrip().startswith(("=", "+", "-", "@")) or any(ord(char) < 32 for char in value)


def _is_totals_or_footer(normal: str) -> bool:
    if re.search(
        r"\b(?:net amount|tax amount|vat amount|total amount|sub total|subtotal|continued|page total"
        # The amount in words, the quantity total and a named total all close the table.  Read
        # as line rows they pollute the last line's description and its item code.
        r"|amount in|amount chargeable|in words|total qty|total quantity"
        r"|(?:gross|net|vat|tax|invoice|grand|page|order|document) total)\b",
        normal,
    ):
        return True
    # A totals row of its own: the word "total" and the figures it adds up, nothing else.
    return bool(re.fullmatch(r"(?:grand |page |invoice )?total(?: [0-9]+(?:\.[0-9]+)?)*", normal))


def _has_continuation_content(parts: dict[str, list[_Word]]) -> bool:
    return bool(parts["item"] or parts["description"])


def _is_near_row(draft: _DraftLine, row: list[_Word], typical_height: float) -> bool:
    # Continuation lines immediately follow their priced row.  A wider limit is
    # still bounded enough to exclude page totals and footers, which are stopped
    # before this check.
    return bool(row) and 0 <= _row_y(row) - draft.last_y <= max(12.0, typical_height * 2.2)


def _merge_continuation(
    draft: _DraftLine, row: list[_Word], columns: _Columns, *, prepend: bool
) -> None:
    parts = _row_parts(row, columns)
    row_text = _row_text(row)
    item = _select_sku(parts["item"])
    description_text = _join_words(parts["description"])
    gtin_match = _GTIN.search(description_text) or _GTIN.search(row_text)
    if gtin_match and draft.gtin is None:
        draft.gtin = gtin_match.group(1).replace(" ", "")
    description = _clean_description(description_text)
    if item and len(item) > 1:
        if draft.sku is None:
            draft.sku = item
        else:
            draft.sku = item + draft.sku if prepend else draft.sku + item
    if description:
        if draft.description:
            draft.description = (
                f"{description} {draft.description}" if prepend else f"{draft.description} {description}"
            )
        else:
            draft.description = description
    if row_text:
        if prepend:
            draft.evidence_parts.insert(0, row_text)
        else:
            draft.evidence_parts.append(row_text)
    draft.last_y = _row_y(row)


def _finish_line(draft: _DraftLine) -> Line:
    evidence = " | ".join(dict.fromkeys(part for part in draft.evidence_parts if part))[:500]
    return Line(
        sku=draft.sku,
        gtin=draft.gtin,
        description=draft.description,
        qty=draft.qty,
        uom=draft.uom,
        price=draft.price,
        net_amount=draft.net_amount,
        tax_amount=draft.tax_amount,
        evidence=evidence or None,
        page=draft.page,
    )
