"""Detect files that bind several invoices: split them by page, hold them for a person, or refuse them.

A file must never lose an invoice silently, so no decision rests on the invoice number alone. Between two pages:

- A new invoice starts on a different printed number or a page counter restarting at 1; on a page that opens like an
  invoice (a title and a date or party block at the top, and amounts below) when the supplier's tax number or letterhead
  changed, or when the page before closed with a total and the header changed; and on a change between native text and a
  scanned page, when the native side stands as an invoice of its own (it closed with a total, or it opens one).
- One such signal alone, or signals that disagree, is unsure: the file stays whole with a blocking hold (hold_pages)
  that a person releases (app/main.py, "Several Invoices"). A part of a file is never exported as if it were all of it.
- A counter that runs on (2 of 3 after 1 of 3), a 'continued' mark or the same printed number continues the invoice.
  A page with no amounts (terms, a blank page) holds nothing to lose and is no signal.

A page that prints two different numbers, or a number that reappears after another one, cannot be split by page and is
refused: the reason names every number so the operator can split the file by hand. A run of scanned pages has no text
at upload: the job child looks at the same signals in what OCR read (suspect_pages), and only ever holds.
Rules are general: they name printed words and shapes, never a supplier.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

MAX_PAGES = 20
MAX_PAGE_CHARS = 20_000
HEADER_LINES = 30  # invoice numbers are read from the top of each page only, never from body references
_HEADING = re.compile(
    r"(?i)\b(?:tax|commercial|sales|proforma|vat)?\s*invoice\s*(?:no\.?|number|num\.?|#|nr\.?)?\s*[:#.]?\s*"
    r"([A-Z0-9][A-Z0-9/-]{3,39})\b"
)
_LABEL_WORDS = {"date", "total", "amount", "number", "type", "details", "summary", "copy", "page"}
_DATE_LIKE = re.compile(r"^\d{1,4}[/-]\d{1,2}[/-]\d{1,4}$")
_PAGE_COUNTER = re.compile(r"(?i)\bpage\s*[:#]?\s*(\d{1,3})\s*(?:of|/)\s*(\d{1,3})\b")
TOP_LINES = 12  # a header block: where a new invoice's title, date and parties print
CLOSE_LINES = 14  # where a total prints, footers below it included
SAME_HEADER_LINES = 6
SCAN_TEXT_CHARS = 40  # fewer characters than this over a page-sized picture is a scan with a stray stamp, not text
_TITLE = re.compile(r"(?i)\b(?:invoice|bill|receipt|credit\s+note|debit\s+note)\b|\u0641\u0627\u062a\u0648\u0631\u0629")
_PARTY = re.compile(r"(?i)\b(?:bill(?:ed)?\s+to|invoice\s+to|ship(?:ped)?\s+to|sold\s+to|deliver(?:ed|y)?\s+to|customer|client|buyer|consignee)\b"
                    r"|\u0627\u0644\u0639\u0645\u064a\u0644|\u0627\u0644\u0645\u0634\u062a\u0631\u064a")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_DATE = re.compile(rf"(?i)\b\d{{1,4}}[./-]\d{{1,2}}[./-]\d{{1,4}}\b|\b\d{{1,2}}[\s-]{_MONTH}[\s,-]+\d{{2,4}}\b|\b{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}\b")
_AMOUNT = re.compile(r"(?<![\d/-])\d{1,3}(?:[,. ]\d{3})+(?:[.,]\d{1,3})?(?![\d/-])|(?<![\d/.-])\d+[.,]\d{2,3}(?![\d/-])")
_TOTAL = re.compile(r"(?i)\b(?:total|payable|amount\s+due|balance\s+due)\b|\u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a|\u0627\u0644\u0645\u062c\u0645\u0648\u0639")
_NOT_TOTAL = re.compile(r"(?i)\bsub[\s-]*total|\b(?:page|running)\s+total|total\s+(?:qty|quantity|items?|lines?|pages?|cartons?|weight|units|pcs|packages)\b"
                        r"|\b(?:carried|brought|forward|c/f|b/f)\b")
_CONTINUED = re.compile(r"(?i)\b(?:continued|contd?\.?|continuation|carried\s+(?:forward|over)|brought\s+forward|b/f|c/f)(?=\W|$)")
# A tax number directly after its label, so a barcode on a row that happens to say 'tin' is not one.
_TAX_ID = re.compile(r"(?i)(?:\b(?:trn|vat|gst|gstin|tax|tin|reg(?:istration)?)\b|\u0627\u0644\u0636\u0631\u064a\u0628\S*)[\s.:#]*(?:(?:reg(?:istration)?|no|number|id)[\s.:#]*){0,3}(\d{9,20})(?!\d)")
_PLAIN_COUNTER = re.compile(r"(?i)^\s*page\s*[:#]?\s*(\d{1,3})\s*$", re.M)
_WORD = re.compile(r"[^\W\d_]{3,}")


@dataclass
class Segment:
    number: str | None
    pages: list[int] = field(default_factory=list)


@dataclass
class Outcome:
    """numbers: distinct invoice numbers in print order; segments: page runs when a clean split exists;
    refusal: the operator-facing reason when numbers were seen but pages cannot be split;
    hold_pages: where a new invoice may start, when the signals are unsure and the file stays whole."""

    numbers: list[str]
    segments: list[Segment]
    refusal: str | None = None
    hold_pages: list[int] = field(default_factory=list)

    @property
    def multiple(self) -> bool:
        return len(self.numbers) > 1 or len(self.segments) > 1


def page_numbers(text: str) -> list[str]:
    """Distinct invoice numbers printed on one page, in print order."""
    seen: list[str] = []
    header = "\n".join(text[:MAX_PAGE_CHARS].splitlines()[:HEADER_LINES])
    for match in _HEADING.finditer(header):
        value = match.group(1).strip().rstrip(".,:;")
        if value.lower() in _LABEL_WORDS or not any(char.isdigit() for char in value) or _DATE_LIKE.match(value):
            continue
        if value not in seen:
            seen.append(value)
    return seen


_VALUE = re.compile(r"(?i)^[A-Z0-9][A-Z0-9/-]{3,39}$")
_NUMBER_WORDS = {"number", "no", "no.", "#", "num", "num.", "nr", "nr."}


def column_numbers(words: list[dict], page_height: float) -> list[str]:
    """Invoice numbers printed under an 'Invoice Number' column heading (the value sits on a row below the
    label, not beside it). Words are pdfplumber words; only headings in the top 40% of the page count, and
    the value is the nearest word below the heading, within four line heights, overlapping its span."""
    found: list[str] = []
    for first, second in zip(words, words[1:]):
        if str(first.get("text", "")).casefold() != "invoice" or str(second.get("text", "")).casefold() not in _NUMBER_WORDS:
            continue
        if abs(first["top"] - second["top"]) > 2 or second["x0"] - first["x1"] > 15 or first["top"] > page_height * 0.4:
            continue
        height = max(1.0, second["bottom"] - second["top"])
        left, right = first["x0"] - 10, second["x1"] + 10
        below = sorted((w for w in words if second["bottom"] < w["top"] <= second["bottom"] + 4 * height
                        and w["x1"] > left and w["x0"] < right), key=lambda w: w["top"])
        for word in below:
            value = str(word.get("text", "")).strip().rstrip(".,:;")
            if _VALUE.match(value) and any(c.isdigit() for c in value) and not _DATE_LIKE.match(value):
                if value not in found:
                    found.append(value)
                break
    return found


def page_counter(text: str) -> tuple[int, int] | None:
    """(page, of). A bare 'Page 2' on a line of its own has of = 0."""
    match = _PAGE_COUNTER.search(text[:MAX_PAGE_CHARS])
    if match:
        return int(match.group(1)), int(match.group(2))
    match = _PLAIN_COUNTER.search(text[:MAX_PAGE_CHARS])
    return (int(match.group(1)), 0) if match else None


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text[:MAX_PAGE_CHARS].splitlines() if line.strip()]


@dataclass
class _Page:
    """What one page shows about where it sits in an invoice."""

    text: str
    scan: bool = False

    def __post_init__(self) -> None:
        lines = _lines(self.text)
        top = lines[:TOP_LINES]
        self.head = [line for line in top[:SAME_HEADER_LINES] if not _PAGE_COUNTER.search(line)]
        self.empty = not lines
        self.dates = {match.group().casefold() for line in top for match in _DATE.finditer(line)}
        titled = any(_TITLE.search(line) for line in top)
        self.tax_ids = {tax for line in top for tax in _TAX_ID.findall(line)}
        self.party = bool(self.tax_ids) or any(_PARTY.search(line) for line in top)
        self.amount_lines = sum(1 for line in lines if _AMOUNT.search(line))
        self.opens = titled and (bool(self.dates) or self.party) and self.amount_lines >= 2
        # A first line that is the title is a running head, not a supplier's name.
        self.letterhead = {word.casefold() for word in _WORD.findall(" ".join(top[:1]))} if top and not _TITLE.search(top[0]) else set()
        self.totals = {_AMOUNT.findall(line)[-1] for line in lines[-CLOSE_LINES:]
                       if _TOTAL.search(line) and not _NOT_TOTAL.search(line) and _AMOUNT.search(line)}
        self.counter = page_counter(self.text)
        self.continues = any(_CONTINUED.search(line) for line in lines[:4])  # runs on from the page before
        self.continues_on = any(_CONTINUED.search(line) for line in lines[-4:])  # runs on to the page after


def _runs_on(previous: _Page, page: _Page) -> bool:
    return bool(previous.counter and page.counter and page.counter == (previous.counter[0] + 1, previous.counter[1]))


def _restarts(previous: _Page, page: _Page) -> bool:
    """A page counter back at 1. 'Page 1' twice with no 'of' may be a counter that never moves: no signal."""
    return bool(page.counter and page.counter[0] == 1 and not (page.counter[1] == 0 and previous.counter == page.counter))


def _between(first: _Page, previous: _Page, page: _Page) -> str | None:
    """'new' when a new invoice starts on ``page``, 'unsure' when one may, None when the invoice runs on."""
    if previous.scan or page.scan:  # a change between native text and a scan: the page below has no text to read yet
        native = page if previous.scan else previous
        if previous.scan and not page.amount_lines:
            return None
        return "new" if (page.opens if previous.scan else bool(native.totals)) else "unsure"
    if _runs_on(previous, page) or page.continues or previous.continues_on or not page.amount_lines:
        return None
    if previous.counter and page.counter and previous.counter[1] and page.counter[1]:  # neither runs on nor restarts
        return "unsure"
    supplier_changed = bool(first.tax_ids and page.tax_ids and not first.tax_ids & page.tax_ids) \
        or bool(first.opens and page.opens and first.letterhead and page.letterhead and not first.letterhead & page.letterhead)
    header_changed = any(line not in first.head for line in page.head)
    if page.opens and (supplier_changed or (previous.totals and header_changed)):
        return "new"
    if supplier_changed:
        return "unsure"
    if page.opens and first.dates and page.dates and not first.dates & page.dates:
        return "unsure"
    if page.opens and page.party and header_changed:  # a full new header, nothing closed before it
        return "unsure"
    if previous.totals and page.totals and not previous.totals & page.totals:  # two different grand totals
        return "unsure"
    return None


def _walk(page_texts: list[str], columns: list[list[str]] | None, scans: list[bool] | None) -> tuple[list[str], list[Segment], list[int], bool]:
    """(numbers, segments, pages where an invoice may start, clean): see the module docstring for the rules."""
    numbers: list[str] = []
    segments: list[Segment] = []
    unsure: list[int] = []
    clean = True
    first = previous = None
    for index, text in enumerate(page_texts[:MAX_PAGES], 1):
        printed = page_numbers(text) or (columns[index - 1] if columns and index <= len(columns) else [])
        if len(printed) > 1:
            clean = False
        for value in printed:
            if value not in numbers:
                numbers.append(value)
        current = segments[-1] if segments else None
        number = printed[0] if printed else None
        page = _Page(text, bool(scans and index <= len(scans) and scans[index - 1]))
        if current is None:
            segments.append(Segment(number, [index]))
            first = previous = None if page.empty and not page.scan else page
            continue
        if page.empty and not page.scan:  # a blank page: no signal, and it says nothing about the next one
            current.pages.append(index)
            continue
        if previous is None:  # only blank pages so far
            current.pages.append(index)
            current.number = current.number or number
            first = previous = page
            continue
        if number is not None and current.number is not None and number != current.number:
            if any(segment.number == number for segment in segments):
                clean = False
            verdict = "unsure" if _runs_on(previous, page) else "new"
        elif number is not None and number == current.number:
            verdict = None
        elif _restarts(previous, page):
            verdict = "new"
        elif page.scan and previous.scan:
            verdict = None
        else:
            verdict = _between(first, previous, page)
        if verdict == "new":
            segments.append(Segment(number, [index]))
            first = page
        else:
            if verdict == "unsure":
                unsure.append(index)
            current.pages.append(index)
            if current.number is None:
                current.number = number
        previous = page
    return numbers, segments, unsure, clean


def analyse_pages(page_texts: list[str], columns: list[list[str]] | None = None, scans: list[bool] | None = None) -> Outcome:
    """Pure analysis of per-page text; see the module docstring for the rules. ``columns`` holds the numbers read under
    an 'Invoice Number' column heading, per page; used where the text names none beside a label. ``scans`` says which
    pages are pictures with no text layer."""
    numbers, segments, unsure, clean = _walk(page_texts, columns, scans)
    whole = [Segment(numbers[0] if numbers else None, list(range(1, len(page_texts[:MAX_PAGES]) + 1)))]
    if not clean and len(numbers) > 1:
        return Outcome(numbers, [], refusal=refusal_message(numbers))
    if unsure:
        return Outcome(numbers, whole, hold_pages=sorted({*unsure, *(segment.pages[0] for segment in segments[1:])}))
    return Outcome(numbers, segments if len(segments) > 1 else whole)


def suspect_pages(page_texts: list[str]) -> list[int]:
    """Where OCR text shows a new invoice may start. Read text is too noisy to split on: this only ever holds."""
    numbers, segments, unsure, clean = _walk(page_texts, None, None)
    pages = {*unsure, *(segment.pages[0] for segment in segments[1:])}
    if not clean and len(numbers) > 1 and not pages:
        pages = {len(page_texts[:MAX_PAGES])}
    return sorted(pages)


def page_texts_from_boxes(boxes: list[dict]) -> list[str]:
    """Per-page text rebuilt from a reader's boxes (OCR text is joined without page marks), top to bottom, a line per row."""
    pages: dict[int, list[dict]] = {}
    for box in boxes:
        place = box.get("box")
        if box.get("geometry") != "word" and place and isinstance(box.get("page"), int) and 1 <= box["page"] <= MAX_PAGES:
            pages.setdefault(box["page"], []).append(box)
    texts = [""] * (max(pages) if pages else 0)
    for number, items in pages.items():
        rows: list[list[dict]] = []
        for item in sorted(items, key=lambda b: b["box"][1]):
            height = max(1.0, item["box"][3] - item["box"][1])
            if rows and item["box"][1] - rows[-1][0]["box"][1] < height * 0.6:
                rows[-1].append(item)
            else:
                rows.append([item])
        texts[number - 1] = "\n".join(" ".join(str(b.get("text", "")) for b in sorted(row, key=lambda b: b["box"][0])) for row in rows)
    return texts


def refusal_message(numbers: list[str]) -> str:
    return (f"This file prints {len(numbers)} invoice numbers ({', '.join(numbers)}) and cannot be split by page. "
            "Split the PDF so each file holds one invoice, then upload the parts.")


def _is_scan(page, text: str) -> bool:
    """A picture covering most of the page, with next to no text over it."""
    if len(text.strip()) >= SCAN_TEXT_CHARS:
        return False
    area = float(page.width) * float(page.height) or 1.0
    return any(abs((image["x1"] - image["x0"]) * (image["bottom"] - image["top"])) >= area / 2 for image in page.images)


def pdf_pages(content: bytes) -> tuple[list[str], list[list[str]], list[bool]]:
    """Native text per page (empty without a text layer), the numbers under an Invoice Number heading, which pages are scans."""
    import pdfplumber

    texts: list[str] = []
    columns: list[list[str]] = []
    scans: list[bool] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages[:MAX_PAGES]:
            try:
                text = (page.extract_text() or "")[:MAX_PAGE_CHARS]
                column = column_numbers(page.extract_words(), float(page.height))
                scan = _is_scan(page, text)
            except Exception:
                text, column, scan = "", [], False
            if scan:
                text = ""
            texts.append(text)
            columns.append(column)
            scans.append(scan)
    return texts, columns, scans


def analyse_pdf(content: bytes) -> Outcome:
    return analyse_pages(*pdf_pages(content))


def outcome_from(data: dict) -> Outcome:
    """The Outcome a reader child answered (engines.split_check), checked before the API process uses it:
    printed invoice numbers only, page runs within the page limit, and the refusal this module would write."""
    numbers, segments, refusal, hold_pages = data["numbers"], data["segments"], data["refusal"], data.get("hold_pages", [])
    if not isinstance(numbers, list) or not all(isinstance(n, str) and _VALUE.fullmatch(n) for n in numbers) \
            or len(set(numbers)) != len(numbers):
        raise ValueError("numbers")
    if not isinstance(segments, list) or not all(
            isinstance(s, dict) and (s["number"] is None or s["number"] in numbers) and isinstance(s["pages"], list)
            and s["pages"] and all(type(p) is int and 1 <= p <= MAX_PAGES for p in s["pages"]) for s in segments):
        raise ValueError("segments")
    if refusal not in (None, refusal_message(numbers)):
        raise ValueError("refusal")
    return Outcome(numbers, [Segment(s["number"], s["pages"]) for s in segments], refusal, clean_pages(hold_pages))


def clean_pages(pages: object) -> list[int]:
    """A page list a child answered: page numbers within the limit, or a refusal."""
    if not isinstance(pages, list) or not all(type(p) is int and 1 <= p <= MAX_PAGES for p in pages):
        raise ValueError("pages")
    return pages


def split_pdf(content: bytes, segments: list[Segment]) -> list[bytes]:
    """One new PDF per segment, pages copied in order."""
    import pypdfium2 as pdfium

    source = pdfium.PdfDocument(content)
    parts: list[bytes] = []
    try:
        for segment in segments:
            target = pdfium.PdfDocument.new()
            try:
                target.import_pages(source, pages=[page - 1 for page in segment.pages])
                buffer = io.BytesIO()
                target.save(buffer)
                parts.append(buffer.getvalue())
            finally:
                target.close()
    finally:
        source.close()
    return parts


def part_filename(filename: str, index: int, count: int, number: str) -> str:
    stem, dot, suffix = filename.rpartition(".")
    base = stem if dot else filename
    safe = re.sub(r"[^A-Za-z0-9._-]", "", number)[:40]
    name = f"{base[:120]} [{index} of {count}{' ' + safe if safe else ''}]"
    return name + (dot + suffix if dot else "")


def split_note(index: int, count: int, numbers: list[str], pages: list[int]) -> str:
    span = f"page {pages[0]}" if len(pages) == 1 else f"pages {pages[0]}-{pages[-1]}"
    named = f" ({', '.join(numbers)})" if numbers else ""
    return (f"The uploaded file bound {count} invoices{named}. It was split by page; "
            f"this record is invoice {index} of {count} ({span} of the original file).")


SEVERAL_CODE = "Several Invoices"


def hold_message(pages: list[int]) -> str:
    where = ", ".join(str(page) for page in pages)
    return (f"This file may hold several invoices: a new one may start on page {where}. It was kept whole and cannot be "
            "exported until you check it. If it holds several, delete this record, split the PDF and upload each invoice. "
            "If it is one invoice, press 'This file is one invoice'.")


def several_pages(job: dict) -> list[int]:
    """Where the file may hold another invoice, from the upload check and the last read, unless a person released it."""
    if job.get("several_released"):
        return []
    return sorted({*(job.get("several_upload") or []), *(job.get("several_read") or [])})


def several_issue(job: dict) -> dict | None:
    pages = several_pages(job)
    return {"code": SEVERAL_CODE, "message": hold_message(pages), "owner": "Accounts payable", "line": None, "rule": "SEVERAL-INVOICES",
            "evidence": "", "blocking": True, "level": "hard"} if pages else None
