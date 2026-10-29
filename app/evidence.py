"""Printed evidence for extracted values: the quote, page and box a value was read from.

Evidence lives beside the invoice on the stored job, never inside it::

    job.evidence = {"header": {field: entry}, "lines": [{field: entry}, ...]}   # lines indexed like invoice.lines
    entry = {"quote": str, "page": int (1-based) | None, "box": [x0, y0, x1, y1] normalised 0-1 (optional),
             "source": "ai" | "ocr" | "native", "review": {"reason": str, "other_value": str | None} (optional)}

Local readers locate each printed value in the page geometry they read (``boxes``). An AI reader quotes
its own sources; on a scan with no text layer a later local OCR pass adds boxes and, where the OCR text
carries a near-identical value instead, a review flag. Nothing here ever changes a value.
"""
import re
from decimal import Decimal, InvalidOperation

from .columnar_tax_invoice import _rows

HEADER_FIELDS = ("number", "supplier_name", "buyer_name", "po", "currency", "net", "tax")
AMOUNT_FIELDS = ("net", "tax")
LINE_FIELDS = ("sku", "gtin", "description", "qty", "price", "net_amount", "tax_amount")
LINE_AMOUNTS = ("qty", "price", "net_amount", "tax_amount")
NEAR_MISS_FIELDS = ("number", "po", "gtin", *AMOUNT_FIELDS, *LINE_AMOUNTS)
MAX_QUOTE = 300
MAX_WINDOW = 12


def _fold(value):
    return re.sub(r"[^0-9a-z]", "", str(value if value is not None else "").casefold())


def _decimal(text):
    try:
        return Decimal(str(text).replace(",", "").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None


def _words(boxes):
    out = []
    for b in boxes or []:
        box, value = b.get("box"), str(b.get("text") or "").strip()
        if not value or not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            x0, top, x1, bottom = (float(v) for v in box)
            page = int(b.get("page") or 1)
            size = b.get("size")
            width, height = (float(size[0]), float(size[1])) if size and len(size) == 2 else (None, None)
        except (TypeError, ValueError):
            continue
        out.append({"text": value, "page": page, "x0": x0, "x1": x1, "top": top, "bottom": bottom,
                    "width": width, "height": height, "fold": _fold(value)})
    return out


def _entry(words):
    quote = " ".join(w["text"] for w in words)[:MAX_QUOTE]
    entry = {"quote": quote, "page": words[0]["page"]}
    width, height = words[0].get("width"), words[0].get("height")
    if width and height:
        x0, y0 = min(w["x0"] for w in words), min(w["top"] for w in words)
        x1, y1 = max(w["x1"] for w in words), max(w["bottom"] for w in words)
        box = [x0 / width, y0 / height, x1 / width, y1 / height]
        if all(0 <= v <= 1 for v in box):
            entry["box"] = [round(v, 4) for v in box]
    return entry


class Layout:
    """The page geometry a reader produced, indexed once so many values can be located cheaply."""

    def __init__(self, boxes):
        self.rows = _rows(_words(boxes))
        self.index = {}
        self.words = []
        for r, row in enumerate(self.rows):
            for k, word in enumerate(row):
                self.words.append(word)
                self.index.setdefault(word["fold"][:2], []).append((r, k))

    def find(self, value, *, numeric=False):
        """The first row window whose printed text is ``value`` (digits and letters compared), or None."""
        target = _fold(value)
        if not target:
            return None
        amount = _decimal(value) if numeric else None
        starts = self.index.get(target[:2], []) + (self.index.get(target[:1], []) if len(target) > 1 else [])
        for r, start in sorted(set(starts)):
            row = self.rows[r]
            folded = ""
            for end in range(start, min(len(row), start + MAX_WINDOW)):
                folded += row[end]["fold"]
                if not folded:
                    continue
                window = row[start:end + 1]
                if folded == target:
                    return _entry(window)
                if amount is not None and len(window) <= 2 and re.search(r"\d", window[-1]["text"]):
                    printed = _decimal("".join(w["text"] for w in window).rstrip(":,"))
                    if printed is not None and printed == amount:
                        return _entry(window)
                if not target.startswith(folded):
                    break
        return None

    def near_miss(self, value):
        """A printed token of the same length differing from ``value`` in exactly one character, or None."""
        target = _fold(value)
        if len(target) < 4:
            return None
        for word in self.words:
            folded = word["fold"]
            if len(folded) == len(target) and sum(a != b for a, b in zip(folded, target)) == 1:
                return word["text"]
        return None


def locate(value, boxes, *, numeric=False):
    return Layout(boxes).find(value, numeric=numeric)


def _value(record, field):
    value = (record or {}).get(field)
    return None if value in (None, "") else value


def _printed_date(invoice):
    return _value(invoice, "date_printed") or _value(invoice, "date")


def header_evidence(invoice, layout, source):
    """Locate every printed header value of ``invoice`` (a dict) in the reader's own page geometry."""
    out = {}
    for field in HEADER_FIELDS:
        value = _value(invoice, field)
        entry = layout.find(value, numeric=field in AMOUNT_FIELDS) if value is not None else None
        if entry:
            out[field] = {**entry, "source": source}
    printed = _printed_date(invoice)
    entry = layout.find(printed) if printed is not None else None
    if entry:
        out["date"] = {**entry, "source": source}
    return out


def line_evidence(lines, layout, source):
    """Per line: the reader's own quote and page for each printed field, with a box when geometry exists."""
    out = []
    for line in lines or []:
        quote, page = line.get("evidence"), line.get("page")
        entries = {}
        for field in LINE_FIELDS:
            value = _value(line, field)
            if value is None:
                continue
            found = layout.find(value, numeric=field in LINE_AMOUNTS) if layout.rows else None
            if found:
                entries[field] = {**found, "source": source}
            elif quote:
                entries[field] = {"quote": str(quote)[:MAX_QUOTE], "page": page, "source": source}
        out.append(entries)
    return out


def build(invoice, boxes, source, header=None):
    """Evidence for a reader's invoice.

    ``header`` is an AI reader's own {field: {quote, page}}; its quotes are kept as given and a box is added
    when the same text is found in the page geometry.
    """
    layout = Layout(boxes)
    located = header_evidence(invoice, layout, source) if layout.rows else {}
    merged = {}
    for field, entry in (header or {}).items():
        if _value(invoice, field) is None and not (field == "date" and _printed_date(invoice)):
            continue
        merged[field] = {**entry, "source": source}
        found = located.get(field)
        if found and (merged[field].get("page") in (None, found["page"])):
            merged[field]["page"] = found["page"]
            if found.get("box"):
                merged[field]["box"] = found["box"]
    for field, entry in located.items():
        merged.setdefault(field, entry)
    return {"header": merged, "lines": line_evidence((invoice or {}).get("lines"), layout, source)}


def _flag(entry, reason, other):
    if "review" not in entry:
        entry["review"] = {"reason": reason, "other_value": None if other is None else str(other)[:MAX_QUOTE]}
    return entry


def verify_with_ocr(invoice, evidence, boxes):
    """Add boxes from a local OCR layer to AI evidence; flag a near-identical OCR value, never replace one."""
    layout = Layout(boxes)
    result = {"header": dict((evidence or {}).get("header") or {}),
              "lines": [dict(x) for x in ((evidence or {}).get("lines") or [])]}
    lines = (invoice or {}).get("lines") or []
    while len(result["lines"]) < len(lines):
        result["lines"].append({})

    def check(target, field, value, numeric, near):
        found = layout.find(value, numeric=numeric)
        entry = dict(target.get(field) or {"quote": None, "page": None, "source": "ai"})
        if found:
            if entry.get("page") is None:
                entry["page"] = found["page"]
            if found.get("box") and entry["page"] == found["page"]:
                entry["box"] = found["box"]
            if entry.get("quote") is None:
                entry["quote"] = found["quote"]
        elif near:
            other = layout.near_miss(value)
            if other is not None:
                _flag(entry, "The local OCR text layer reads a different value here", other)
        if found or "review" in entry or field in target:
            target[field] = entry

    for field in HEADER_FIELDS:
        value = _value(invoice, field)
        if value is not None:
            check(result["header"], field, value, field in AMOUNT_FIELDS, field in NEAR_MISS_FIELDS)
    printed = _printed_date(invoice)
    if printed is not None:
        check(result["header"], "date", printed, False, False)
    for index, line in enumerate(lines):
        for field in LINE_FIELDS:
            value = _value(line, field)
            if value is not None:
                check(result["lines"][index], field, value, field in LINE_AMOUNTS, field in NEAR_MISS_FIELDS)
    return result


def filename_review(number, filename):
    """Flag an invoice number one character away from a digit run in the file name; never correct it."""
    digits = re.sub(r"\D", "", str(number or ""))
    runs = re.findall(r"\d{5,}", str(filename or ""))
    if len(digits) < 5 or not runs or any(run in digits or digits in run for run in runs):
        return None
    for run in runs:
        if len(run) == len(digits) and sum(a != b for a, b in zip(run, digits)) == 1:
            return {"reason": "The file name carries digits one character away from the invoice number",
                    "other_value": run}
    return None


def annotate(evidence, invoice, *, filename=None, source="ai"):
    """Apply the value-independent checks to a job's evidence (today: the file-name digit cross-check)."""
    result = {"header": dict((evidence or {}).get("header") or {}),
              "lines": list((evidence or {}).get("lines") or [])}
    review = filename_review(_value(invoice, "number"), filename)
    if review:
        entry = dict(result["header"].get("number") or {"quote": None, "page": None, "source": source})
        result["header"]["number"] = _flag(entry, review["reason"], review["other_value"])
    return result
