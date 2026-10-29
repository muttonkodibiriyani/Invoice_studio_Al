"""Detect PDFs that bind several invoices and split them by page, or refuse them with both numbers named.

Only the native text layer is read: a number beside an invoice-number label, or (when none) the value
printed under an 'Invoice Number' column heading. A page starts a new invoice when it prints a different invoice
number from the running one, or when its page counter restarts at 1 after the first page. Pages that
print no number continue the running invoice. A page that prints two different numbers, or a number
that reappears after another one, cannot be split by page and is refused: the reason names every
number so the operator can split the file by hand. Scans (no text layer) are left to the readers.
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


@dataclass
class Segment:
    number: str | None
    pages: list[int] = field(default_factory=list)


@dataclass
class Outcome:
    """numbers: distinct invoice numbers in print order; segments: page runs when a clean split exists;
    refusal: the operator-facing reason when numbers were seen but pages cannot be split."""

    numbers: list[str]
    segments: list[Segment]
    refusal: str | None = None

    @property
    def multiple(self) -> bool:
        return len(self.numbers) > 1


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
    match = _PAGE_COUNTER.search(text[:MAX_PAGE_CHARS])
    return (int(match.group(1)), int(match.group(2))) if match else None


def analyse_pages(page_texts: list[str], columns: list[list[str]] | None = None) -> Outcome:
    """Pure analysis of per-page text; see the module docstring for the rules. ``columns`` holds the numbers
    read under an 'Invoice Number' column heading, per page; used where the text names none beside a label."""
    numbers: list[str] = []
    segments: list[Segment] = []
    clean = True
    for index, text in enumerate(page_texts[:MAX_PAGES], 1):
        printed = page_numbers(text) or (columns[index - 1] if columns and index <= len(columns) else [])
        if len(printed) > 1:
            clean = False
        for value in printed:
            if value not in numbers:
                numbers.append(value)
        current = segments[-1] if segments else None
        number = printed[0] if printed else None
        counter = page_counter(text)
        restarts = counter is not None and counter[0] == 1 and index > 1
        if current is None:
            segments.append(Segment(number, [index]))
        elif number is not None and current.number is None and not restarts:
            current.number = number
            current.pages.append(index)
        elif number is not None and number != current.number:
            if any(segment.number == number for segment in segments):
                clean = False
            segments.append(Segment(number, [index]))
        elif restarts and number is None:
            segments.append(Segment(None, [index]))
        else:
            current.pages.append(index)
    if len(numbers) < 2:
        return Outcome(numbers, [Segment(numbers[0] if numbers else None, list(range(1, len(page_texts[:MAX_PAGES]) + 1)))])
    if not clean or any(segment.number is None for segment in segments) or len(segments) != len(numbers):
        return Outcome(numbers, [], refusal=refusal_message(numbers))
    return Outcome(numbers, segments)


def refusal_message(numbers: list[str]) -> str:
    return (f"This file prints {len(numbers)} invoice numbers ({', '.join(numbers)}) and cannot be split by page. "
            "Split the PDF so each file holds one invoice, then upload the parts.")


def pdf_pages(content: bytes) -> tuple[list[str], list[list[str]]]:
    """Native text per page (empty without a text layer) and the numbers under an Invoice Number heading."""
    import pdfplumber

    texts: list[str] = []
    columns: list[list[str]] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages[:MAX_PAGES]:
            try:
                text = (page.extract_text() or "")[:MAX_PAGE_CHARS]
                column = column_numbers(page.extract_words(), float(page.height))
            except Exception:
                text, column = "", []
            texts.append(text)
            columns.append(column)
    return texts, columns


def pdf_page_texts(content: bytes) -> list[str]:
    """Native text per page, empty strings for pages without a text layer."""
    return pdf_pages(content)[0]


def analyse_pdf(content: bytes) -> Outcome:
    return analyse_pages(*pdf_pages(content))


def outcome_from(data: dict) -> Outcome:
    """The Outcome a reader child answered (engines.split_check), checked before the API process uses it:
    printed invoice numbers only, page runs within the page limit, and the refusal this module would write."""
    numbers, segments, refusal = data["numbers"], data["segments"], data["refusal"]
    if not isinstance(numbers, list) or not all(isinstance(n, str) and _VALUE.fullmatch(n) for n in numbers) \
            or len(set(numbers)) != len(numbers):
        raise ValueError("numbers")
    if not isinstance(segments, list) or not all(
            isinstance(s, dict) and (s["number"] is None or s["number"] in numbers) and isinstance(s["pages"], list)
            and s["pages"] and all(type(p) is int and 1 <= p <= MAX_PAGES for p in s["pages"]) for s in segments):
        raise ValueError("segments")
    if refusal not in (None, refusal_message(numbers)):
        raise ValueError("refusal")
    return Outcome(numbers, [Segment(s["number"], s["pages"]) for s in segments], refusal)


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
    name = f"{base[:120]} [{index} of {count} {safe}]"
    return name + (dot + suffix if dot else "")


def split_note(index: int, count: int, numbers: list[str], pages: list[int]) -> str:
    span = f"page {pages[0]}" if len(pages) == 1 else f"pages {pages[0]}-{pages[-1]}"
    return (f"The uploaded file bound {count} invoices ({', '.join(numbers)}). It was split by page; "
            f"this record is invoice {index} of {count} ({span} of the original file).")
