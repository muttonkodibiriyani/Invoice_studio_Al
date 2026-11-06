"""Find invoice rows from their arithmetic, for a table whose headings no reader recognised.

A priced row prints two numbers that multiply to a third, in columns that repeat down the page.
That needs no word list, so it holds for any wording. It is also easy to fool: a tax summary, a
totals block, a tariff code or a page number can happen to multiply. So only a table the page
itself confirms is read, and a wrong row being worse than no row:

- rows naming a total or a tax are never rows, and the table ends at the first one;
- a table is confirmed by two rows that show real arithmetic in the same columns, or by rows whose
  amounts add up to a figure printed below them. "1 x 500.00 = 500.00" proves nothing on its own;
- a row inside the table that does not multiply is kept with its quantity and price empty, which
  the line rules flag, and so is every row when quantity and price cannot be told apart.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .layout_extract import (
    _clean_description,
    _clean_uom,
    _group_all_rows,
    _is_totals_or_footer,
    _normal,
    _row_text,
    _SAFE_IDENTIFIER,
    _Word,
)
from .models import Line, uom_code
from .numbers import parse_number

# Two columns are one when an edge or the centre of their figures lines up, as a share of the page width.
_COLUMN_TOLERANCE = 0.02
_MAX_ROW_NUMBERS = 14
_LETTER = re.compile(r"[^\W\d_]")
_ROW_CLOSER = re.compile(
    r"\b(?:total|totals|subtotal|vat|tax|taxable|gst|balance|carried|brought|rounding|round off|summary)\b"
)
_SIGNS = "€$£₹¥"
_INDIAN_GROUPS = re.compile(r"[0-9]{1,2}(?:,[0-9]{2})+,[0-9]{3}(?:\.[0-9]+)?")
_LOST_GROUP_MARKS = re.compile(r"[0-9]{1,3}(?:\.[0-9]{3})+\.[0-9]{2}")
_WITH_UNIT = re.compile(r"([0-9]+(?:[.,][0-9]+)?)([A-Za-z]{1,6})\.?")
_MEASURE = re.compile(r"(?i)[0-9]+(?:[.,][0-9]+)?(?:x[0-9]+)?(?:mm|cm|m|ml|cl|l|g|gm|kg|inch|in|oz|w|v)")
_GLUED = re.compile(r"([0-9][0-9,]*[.,][0-9]{%d})([0-9][0-9,]*[.,][0-9]+)")
_CURRENCY_WORD = re.compile(r"(?i)(?:AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR|INR|EGP|JPY|Rs\.?|Dhs?\.?|[€$£₹¥])")
# What a document calling itself one of these is not, however well its rows multiply.
_OTHER_DOCUMENT = re.compile(
    r"(?im)^[\s#]*(?:(?:sales\s+|price\s+)?quot(?:e|ation)|pro\s*-?\s*forma(?:\s+invoice)?|estimate"
    r"|delivery\s+(?:note|order)|goods\s+received(?:\s+note)?|packing\s+(?:list|slip)"
    r"|(?:account\s+)?statement(?:\s+of\s+account)?|purchase\s+order|order\s+confirmation)[\s:.]*$"
)
# The title on a line of its own, as "Credit Note" or "Sales Return Note"; "Credit Note No: 12" against an invoice is
# a reference. Wording a credit under no such title reads as printed: the rules still see its printed signs.
_CREDIT_TITLE = re.compile(
    r"(?im)^[\s#*]*(?:\w+\s+){0,2}?(?:credit|return|refund)s?\s+(?:note|memo|invoice|voucher|advice|adjustment)[\s:.*]*$"
)
_UOM_SHAPE = re.compile(r"[A-Z]{1,5}\.?")
# A share of the rows that must agree before a text column is read as codes or units.
_COLUMN_VOTE = 0.6
_ROUNDING_SHARE = Decimal("0.005")
_PRESENTATION_FORM = re.compile("[\ufb50-\ufdff\ufe70-\ufeff]")
_FIGURE_END = re.compile(r"^\d[\d,]*\.\d+$")


@dataclass(frozen=True)
class _Number:
    value: Decimal
    decimals: int
    words: tuple[int, ...]
    x0: float
    x1: float
    unit: str | None = None
    percent: bool = False
    # Which split of a word printing two figures touching this is, and which piece of it.
    piece: tuple[int, int] | None = None
    # The mark before exactly three last digits, which can be a point or a thousands group.
    mark: str | None = None

    def lines_up(self, other: "_Number", tolerance: float) -> bool:
        return (
            abs(self.x1 - other.x1) <= tolerance
            or abs(self.x0 - other.x0) <= tolerance
            or abs((self.x0 + self.x1) - (other.x0 + other.x1)) / 2 <= tolerance
        )


@dataclass(frozen=True)
class _Product:
    left: _Number
    right: _Number
    amount: _Number
    discount: _Number | None = None

    @property
    def strong(self) -> bool:
        """Real arithmetic: nothing is multiplied by one, and the amount is not nothing."""
        return bool(self.amount.value) and abs(self.left.value) != 1 and abs(self.right.value) != 1

    def lines_up(self, other: "_Product", tolerance: float) -> bool:
        return (
            self.left.lines_up(other.left, tolerance)
            and self.right.lines_up(other.right, tolerance)
            and self.amount.lines_up(other.amount, tolerance)
        )

    @property
    def used(self) -> set[int]:
        parts = (self.left, self.right, self.amount) + ((self.discount,) if self.discount else ())
        return {index for part in parts for index in part.words}


def _parse(text: str) -> str | None:
    text = text.strip().strip(_SIGNS).replace("'", "").replace("’", "")
    if _INDIAN_GROUPS.fullmatch(text):
        text = text.replace(",", "")
    # "510.563.14": only a scan prints this; the group marks read as points, and the last one is the decimal.
    if _LOST_GROUP_MARKS.fullmatch(text):
        head, _, tail = text.rpartition(".")
        text = f"{head.replace('.', '')}.{tail}"
    return parse_number(text)


def _number(text: str, words: tuple[int, ...], x0: float, x1: float, **kind) -> _Number | None:
    plain = _parse(text)
    if plain is None:
        return None
    try:
        value = Decimal(plain)
    except InvalidOperation:
        return None
    mark = re.search(r"([.,])[0-9]{3}$", text.strip())
    return _Number(value, len(plain.partition(".")[2]), words, x0, x1, mark=mark[1] if mark else None, **kind)


def _is_barcode(text: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{12,}", text)) or (bool(re.fullmatch(r"[0-9]{8}", text)) and _check_digit_ok(text))


def _check_digit_ok(digits: str) -> bool:
    total = sum(int(digit) * (3 if place % 2 else 1) for place, digit in enumerate(reversed(digits)))
    return total % 10 == 0


def _as_printed(row: list[_Word]) -> list[_Word]:
    """The row's words as the page spaces them. A reader that splits "2,423.34" into "2", ",", "423.34"
    reports the line each came from, and that line says which of them touch."""
    merged: list[_Word] = []
    for word in row:
        previous = merged[-1] if merged else None
        if previous is not None and word.line and previous.line == word.line and _touching(previous, word):
            merged[-1] = replace(previous, text=previous.text + word.text, x1=word.x1)
        else:
            merged.append(word)
    return merged


def _touching(first: _Word, second: _Word) -> bool:
    """Whether the first word, read so far, runs straight into the second somewhere on their line."""
    tokens = (first.line or "").split()
    start, wanted = first.text, second.text
    return any(token.startswith(start + wanted) or start + wanted in token for token in tokens) or bool(
        # A reader can space the marks of one number apart: "136 , 813.68".
        re.fullmatch(r"[0-9.,]*[0-9][.,]?", start) and re.fullmatch(r"[.,]|[.,]?[0-9][0-9.,]*", wanted)
        and (start[-1] in ".," or wanted[0] in ".,")
    )


def _reversed_digits(text: str) -> str | None:
    """A right-to-left run of Arabic digits can reach us back to front."""
    return text[::-1] if re.search(r"[٠-٩۰-۹]", text) else None


def _row_numbers(row: list[_Word], width: float) -> list[_Number]:
    """Every number a row can be read as printing. One word can be two figures printed touching,
    and two words one figure grouped by a space; the arithmetic decides which, not this."""
    found: list[_Number] = []
    for index, word in enumerate(row):
        text = word.text.strip()
        x0, x1 = word.x0 / width, word.x1 / width
        if text.endswith("%"):
            percent = _number(text[:-1], (index,), x0, x1, percent=True)
            if percent is not None:
                found.append(percent)
            continue
        if _is_barcode(text):
            continue
        whole = _number(text, (index,), x0, x1)
        if whole is not None:
            found.append(whole)
            # "1.200" reads as 1.2, but dots can group thousands: the arithmetic may need 1200.
            if re.fullmatch(r"[0-9]{1,3}\.[0-9]{3}", text):
                other = _number(text.replace(".", ""), (index,), x0, x1)
                if other is not None:
                    found.append(other)
            # "8.054.144": a poor scan can print the group mark as a point, so the last one may be the decimal.
            if re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{3}){2,}", text):
                head, _, tail = text.rpartition(".")
                other = _number(f"{head.replace('.', '')}.{tail}", (index,), x0, x1)
                if other is not None:
                    found.append(other)
            mirrored = _reversed_digits(text)
            other = _number(mirrored, (index,), x0, x1) if mirrored else None
            if other is not None:
                found.append(other)
            continue
        unit = _WITH_UNIT.fullmatch(text)
        if unit is not None and (unit[2].isupper() or not _MEASURE.fullmatch(text)):
            counted = _number(unit[1], (index,), x0, x1, unit=unit[2])
            if counted is not None:
                found.append(counted)
            continue
        for places in (2, 3):
            glued = re.fullmatch(_GLUED.pattern % places, text)
            if glued is None:
                continue
            first = _number(glued[1], (index,), x0, x0, piece=(places, 1))
            second = _number(glued[2], (index,), x1, x1, piece=(places, 2))
            if first is not None and second is not None:
                found += [first, second]
    for start in range(len(row) - 1):
        joined, end = row[start].text.strip(), start
        if not re.fullmatch(r"[0-9]{1,3}", joined):
            continue
        while end + 1 < len(row) and re.fullmatch(r"[0-9]{3}(?:[.,][0-9]+)?", row[end + 1].text.strip()):
            end += 1
            joined += row[end].text.strip()
            number = _number(joined, tuple(range(start, end + 1)), row[start].x0 / width, row[end].x1 / width)
            if number is not None:
                found.append(number)
    return found[:_MAX_ROW_NUMBERS]


def _unit_size(decimals: int) -> Decimal:
    return Decimal(1).scaleb(-decimals)


def _agrees(result: Decimal, printed: _Number, left: _Number, right: _Number) -> bool:
    """A printed figure is rounded, and so can be each factor; whole factors are taken as exact."""
    half = lambda number: _unit_size(number.decimals) / 2 if number.decimals else Decimal(0)
    slack = half(printed) + _unit_size(printed.decimals) / 2 * (not printed.decimals)
    slack += abs(left.value) * half(right) + abs(right.value) * half(left)
    return abs(abs(result) - abs(printed.value)) <= min(slack, _unit_size(printed.decimals) + abs(printed.value) / 200)


def _disjoint(*numbers: _Number) -> bool:
    """No word is read twice, except the two pieces of one split of a word."""
    for n, number in enumerate(numbers):
        for other in numbers[n + 1 :]:
            if set(number.words) & set(other.words) and not (
                number.piece and other.piece and number.piece[0] == other.piece[0] and number.piece[1] != other.piece[1]
            ):
                return False
    return True


def _products(numbers: list[_Number]) -> list[_Product]:
    plain = [number for number in numbers if not number.percent]
    found: list[_Product] = []
    for i, first in enumerate(plain):
        for second in plain[i + 1:]:
            if not first.value or not _disjoint(first, second):
                continue
            left, right = (first, second) if first.x0 <= second.x0 else (second, first)
            product = left.value * right.value
            for amount in plain:
                if amount.unit is None and _disjoint(left, right, amount) and _agrees(product, amount, left, right):
                    found.append(_Product(left, right, amount))
    if found:
        return found
    # No plain product: a printed discount may stand between them.
    for i, first in enumerate(plain):
        for second in plain[i + 1:]:
            if not first.value or not second.value or not _disjoint(first, second):
                continue
            left, right = (first, second) if first.x0 <= second.x0 else (second, first)
            product = left.value * right.value
            for amount in plain:
                if amount.unit is not None or not amount.value or not _disjoint(left, right, amount):
                    continue
                for off in numbers:
                    if not _disjoint(left, right, amount, off) or not 0 < off.value:
                        continue
                    results = [product * (1 - off.value / 100)] if off.value < 100 else []
                    if not off.percent:
                        results.append(product - off.value)
                    if any(_agrees(result, amount, left, right) for result in results):
                        found.append(_Product(left, right, amount, off))
    return found


def _settled(product: _Product, numbers: list[_Number]) -> tuple[_Number, Decimal | None]:
    """The row's net amount and tax. A row can go on past the product: less a discount to an amount
    after it, or plus a tax to a gross. The amount the row settles on is the one the table adds up."""
    amount, tax = product.amount, None
    if product.discount is not None:
        return amount, None
    rest = [
        number for number in numbers
        if _disjoint(product.left, product.right, product.amount, number) and number.value > 0
    ]
    for change in rest:
        for after in rest:
            if not _disjoint(change, after) or after.percent:
                continue
            base = abs(amount.value)
            step = base * change.value / 100 if change.percent else change.value
            if after.value < base and _agrees(base - step, after, amount, change):
                return after, None
    for change in rest:
        if change.percent:
            continue
        for rate in rest:
            if rate.percent and abs(abs(amount.value) * rate.value / 100 - change.value) <= _unit_size(change.decimals):
                tax = change.value
    if tax is None:
        for change in rest:
            for after in rest:
                if change.percent or after.percent or not _disjoint(change, after):
                    continue
                if after.value == abs(amount.value) + change.value and change.value < abs(amount.value):
                    tax = change.value
    return amount, tax


@dataclass
class _Row:
    page: int
    words: list[_Word]
    numbers: list[_Number]
    products: list[_Product]
    closes: bool
    y: float


def _build_rows(words: list[_Word]) -> list[_Row]:
    rows: list[_Row] = []
    for page, row in _group_all_rows(words):
        ordered = _as_printed(sorted((word for word in row if word.text.strip()), key=lambda word: word.x0))
        if not ordered:
            continue
        normal = _normal(_row_text(ordered))
        numbers = _row_numbers(ordered, ordered[0].page_width)
        closes = _is_totals_or_footer(normal) or bool(_ROW_CLOSER.search(normal))
        products = [] if closes or not any(_LETTER.search(word.text) for word in ordered) else _products(numbers)
        rows.append(_Row(page, ordered, numbers, products, closes, min(word.y0 for word in ordered)))
    return rows


def _best_shape(rows: list[_Row]) -> _Product | None:
    """The one set of columns most rows multiply in, by the rows with real arithmetic first."""
    best, score = None, (0, 0)
    tried: list[_Product] = []
    for row in rows:
        for product in row.products:
            if any(product.lines_up(other, _COLUMN_TOLERANCE) for other in tried):
                continue
            tried.append(product)
            matching = [_fitting(candidate, product) for candidate in rows]
            found = (sum(any(p.strong for p in fit) for fit in matching), sum(bool(fit) for fit in matching))
            if found > score:
                best, score = product, found
    return best


def _fitting(row: _Row, shape: _Product) -> list[_Product]:
    return [product for product in row.products if product.lines_up(shape, _COLUMN_TOLERANCE)]


def _out_of_line(row: _Row, shape: _Product) -> list[_Product]:
    """Free-flowing text shifts a row's figures out of the columns. Real arithmetic ending the row, in the order the
    table prints its figures, is the same reading."""
    amount_last = shape.amount.x0 > max(shape.left.x0, shape.right.x0)
    if not amount_last or not row.numbers:
        return []
    last = max(row.numbers, key=lambda number: number.x0)
    return [product for product in row.products if product.strong and product.amount is last]


def _table_rows(rows: list[_Row], shape: _Product) -> list[_Row]:
    """Per page, the rows from the first that multiplies to the last before anything closes the table."""
    kept: list[_Row] = []
    for page in dict.fromkeys(row.page for row in rows):
        on_page = [row for row in rows if row.page == page]
        fits = [n for n, row in enumerate(on_page) if _fitting(row, shape)]
        if not fits:
            continue
        end = next((n for n in range(fits[0], len(on_page)) if on_page[n].closes), len(on_page))
        fits = [n for n in fits if n < end]
        if fits:
            kept.extend(on_page[fits[0] : fits[-1] + 1])
    return kept


def _amount_of(row: _Row, shape: _Product) -> _Number | None:
    fits = _fitting(row, shape)
    if fits:
        return fits[0].amount
    return next((n for n in row.numbers if not n.percent and n.lines_up(shape.amount, _COLUMN_TOLERANCE)), None)


def _sum_printed_below(rows: list[_Row], table: list[_Row], shape: _Product) -> bool:
    """The amounts add up to a figure printed below them, as exactly as the amounts print."""
    settled = [_settled(_fitting(row, shape)[0], row.numbers)[0] for row in table if _fitting(row, shape)]
    total = abs(sum((number.value for number in settled), Decimal(0)))
    last = table[-1]
    below = [row for row in rows if (row.page, row.y) > (last.page, last.y)]
    # Zero is printed everywhere: nothing adding up to it says much without a row closing the table.
    if not total:
        return len(settled) >= 2 and any(
            not number.value and number.decimals for row in below if row.closes for number in row.numbers
        )
    return any(
        abs(number.value) == total and number.decimals == settled[0].decimals
        for row in below for number in row.numbers if not number.percent
    )


def _is_tax_summary(rows: list[_Row], table: list[_Row], shape: _Product) -> bool:
    """Rate x taxable = tax also multiplies. A rate printed as 0.05 beside a base with decimals, or a
    product headed as a tax, is a summary of the table and not the table."""
    products = [fits[0] for fits in (_fitting(row, shape) for row in table) if fits]
    for rates, bases in (([p.left for p in products], [p.right for p in products]),
                         ([p.right for p in products], [p.left for p in products])):
        if all(abs(n.value) < 1 for n in rates) and all(n.decimals for n in bases):
            return True
    above = [row for row in rows if row.page == table[0].page and row.y < table[0].y][-1:]
    return any(
        re.search(r"(?i)\b(?:vat|tax|gst)\b", word.text)
        and word.x0 / row.words[0].page_width - _COLUMN_TOLERANCE <= shape.amount.x1
        and shape.amount.x0 <= word.x1 / row.words[0].page_width + _COLUMN_TOLERANCE
        for row in above for word in row.words
    )


def _confirmed(rows: list[_Row], table: list[_Row], shape: _Product) -> bool:
    if _is_tax_summary(rows, table, shape):
        return False
    fits = [_fitting(row, shape) for row in table]
    return sum(any(p.strong for p in fit) for fit in fits) >= 2 or _sum_printed_below(rows, table, shape)


def _heading_role(rows: list[_Row], table: list[_Row], number: _Number) -> str | None:
    """What the heading above a column calls it, when any heading above the table does."""
    from .docling_extract import _role

    above = [row for row in rows if row.page == table[0].page and row.y < table[0].y]
    for row in reversed(above[-3:]):
        width = row.words[0].page_width
        lettered = [word for word in row.words if _LETTER.search(word.text)]
        if len(lettered) < 2:
            continue
        over = [
            word for word in lettered
            if word.x0 / width - _COLUMN_TOLERANCE <= number.x1 and number.x0 <= word.x1 / width + _COLUMN_TOLERANCE
        ]
        if over:
            return _role(" ".join(word.text for word in over))
    return None


def _quantity_side(rows: list[_Row], table: list[_Row], shape: _Product) -> str | None:
    """Which factor column is the quantity, or None when nothing tells: amounts multiply either way round."""
    products = [fit[0] for fit in (_fitting(row, shape) for row in table) if fit]
    lefts, rights = [p.left for p in products], [p.right for p in products]
    if any(n.unit for n in lefts) != any(n.unit for n in rights):
        return "left" if any(n.unit for n in lefts) else "right"
    # A money column prints the decimals its amounts print; a count need not.
    money = lambda column: all(n.decimals == p.amount.decimals for n, p in zip(column, products))
    if money(lefts) != money(rights):
        return "right" if money(lefts) else "left"
    # A money column prints the same decimals on every row; a count that varies does not.
    even = lambda column: len({n.decimals for n in column}) == 1
    if even(lefts) != even(rights):
        return "right" if even(lefts) else "left"
    if {lefts[0].decimals, rights[0].decimals} in ({0, 2}, {0, 3}):
        return "left" if lefts[0].decimals == 0 else "right"
    whole = lambda column: all(n.value == n.value.to_integral_value() for n in column)
    if whole(lefts) != whole(rights):
        return "left" if whole(lefts) else "right"
    left_role = _heading_role(rows, table, shape.left)
    right_role = _heading_role(rows, table, shape.right)
    if left_role in ("qty", "price") and right_role not in ("qty", "price"):
        return "left" if left_role == "qty" else "right"
    if right_role in ("qty", "price") and left_role not in ("qty", "price"):
        return "right" if right_role == "qty" else "left"
    if {left_role, right_role} == {"qty", "price"}:
        return "left" if left_role == "qty" else "right"
    return None


@dataclass
class _Text:
    """What a row prints besides its figures, split on those figures."""

    segments: list[list[str]]
    gtin: str | None = None


def _text_of(row: _Row, used: set[int]) -> _Text:
    segments: list[list[str]] = [[]]
    gtin = None
    for index, word in enumerate(row.words):
        text = word.text.strip()
        if index in used:
            segments.append([])
        elif re.fullmatch(r"[0-9]{8,14}", text) and gtin is None:
            gtin = text
        elif text.endswith("%") or _CURRENCY_WORD.fullmatch(text) or text in {"x", "X", "@", "×", "|", ":", "CR"}:
            continue
        else:
            segments[-1].append(text)
    return _Text([segment for segment in segments if segment], gtin)


def _main_segment(text: _Text) -> list[str]:
    if not text.segments:
        return []
    return max(text.segments, key=lambda segment: sum(1 for word in segment if _LETTER.search(word)))


def _wraps_under(row: _Row, above: _Row, below: _Row) -> bool:
    """A row with no figures continues the text above it only when it starts where that text does, right below it."""
    text = [word for word in row.words if _LETTER.search(word.text) and not _is_code(word.text.strip())]
    close = below.page == above.page and below.y - above.y <= 2.5 * (above.words[0].y1 - above.words[0].y0)
    return bool(text) and close and abs(below.words[0].x0 - text[0].x0) <= _COLUMN_TOLERANCE


def _letter_words(text: _Text) -> int:
    return sum(1 for segment in text.segments for word in segment if _LETTER.search(word))


def _with_row_above(rows: list[_Row], row: _Row, text: _Text) -> _Text:
    above = [other for other in rows if other.page == row.page and other.y < row.y][-1:]
    if not above or above[0].closes or above[0].products or not any(_LETTER.search(word.text) for word in above[0].words):
        return text
    printed = _text_of(above[0], set())
    return _Text(printed.segments + text.segments, printed.gtin or text.gtin)


def _is_code(word: str) -> bool:
    value = word.replace(" ", "")
    return (
        bool(_SAFE_IDENTIFIER.fullmatch(value))
        and any(character.isdigit() for character in value)
        and not _MEASURE.fullmatch(value)
        and not _FIGURE_END.fullmatch(value)
    )


def _is_unit(word: str) -> bool:
    return bool(_UOM_SHAPE.fullmatch(word)) or uom_code(word) != word.strip(". ").upper()


def _serial_column(texts: list[_Text]) -> bool:
    """A first column counting 1, 2, 3 is a row number, not a code."""
    firsts = [text.segments[0][0] if text.segments else "" for text in texts]
    return len(firsts) > 1 and all(value == str(n + 1) for n, value in enumerate(firsts))


def _logical_order(words: list[str]) -> list[str]:
    """A PDF text layer can hold Arabic as the glyphs on the page, left to right: put it back as the words it spells."""
    if not words or not all(_PRESENTATION_FORM.search(word) or word.isdigit() for word in words):
        return words
    return [unicodedata.normalize("NFKC", word) for word in reversed(words)]


def _describe(texts: list[_Text]) -> list[dict[str, str | None]]:
    """Code, unit and description per row. A code or unit counts only when most rows print one in that place."""
    if _serial_column(texts):
        texts = [_Text([segment[1:] if n == 0 else segment for n, segment in enumerate(text.segments)], text.gtin)
                 for text in texts]
    mains = [_main_segment(text) for text in texts]
    size = max(1, len(mains))
    codes_first = sum(bool(main) and _is_code(main[0]) for main in mains) / size >= _COLUMN_VOTE
    units_last = sum(len(main) > 1 and _is_unit(main[-1]) for main in mains) / size >= _COLUMN_VOTE
    described: list[dict[str, str | None]] = []
    for text, main in zip(texts, mains):
        words = list(main)
        code = words.pop(0) if codes_first and words and _is_code(words[0]) else None
        unit = words.pop() if units_last and len(words) > 1 and _is_unit(words[-1]) else None
        for other in text.segments:
            if other is main:
                continue
            for word in other:
                if code is None and _is_code(word):
                    code = word
                elif unit is None and _is_unit(word):
                    unit = word
        described.append({"sku": code, "uom": unit, "description": " ".join(_logical_order(words)), "gtin": text.gtin})
    return described


def _marked_credit(row: _Row, amount: _Number) -> bool:
    """An amount printed with CR after it is a credit: "37087.20 CR"."""
    if amount is None:
        return False
    last = max(amount.words)
    return last + 1 < len(row.words) and row.words[last + 1].text.strip().casefold().strip(".") == "cr"


def credit_signed(invoice: dict[str, Any], text: str) -> dict[str, Any]:
    """A document titled a credit or a return takes money back whatever sign it prints, so its amounts are negative."""
    if not _CREDIT_TITLE.search(text or ""):
        return invoice
    def negative(value: Any) -> Any:
        number = _as_decimal(value)
        return value if number is None else -abs(number)
    return {
        **invoice,
        **{field: negative(invoice.get(field)) for field in ("net", "tax", "gross") if field in invoice},
        "lines": [{**line, "net_amount": negative(line.get("net_amount")), "tax_amount": negative(line.get("tax_amount"))}
                  for line in invoice.get("lines") or []],
    }


def names_another_document(text: str) -> bool:
    """A line of its own says the document is a quotation, an order, a statement or the like. The same words
    with a value after them are a reference an invoice prints: "Purchase Order: 4471"."""
    return bool(_OTHER_DOCUMENT.search(text))


def _decimals(product: _Product) -> int:
    return product.left.decimals + product.right.decimals + product.amount.decimals


def _table_prints_decimal_points(table: list[_Row], shape: _Product) -> bool:
    """A row whose only reading has a point before three last digits says what that mark means in "34.768" on the others."""
    return any(
        number.mark == "." and number.decimals == 3
        for row in table
        for fits in [_fitting(row, shape)]
        if len(fits) == 1
        for number in (fits[0].left, fits[0].right, fits[0].amount)
    )


def _marks_disagree(products: list[_Product]) -> bool:
    """A column printing "16.156" on one row and "5,359" on the next: a scan has lost which mark is which,
    and the same digits multiply just as well a thousand times larger, so no price or amount is certain."""
    for column in (
        [p.left for p in products],
        [p.right for p in products],
        [p.amount for p in products],
    ):
        if len({number.mark for number in column if number.mark}) > 1:
            return True
    return False


def read_lines(words: Iterable[_Word]) -> list[Line]:
    """The lines of a table found by its arithmetic; empty unless the page confirms the table."""
    rows = _build_rows(list(words))
    shape = _best_shape(rows)
    if shape is None:
        return []
    table = _table_rows(rows, shape)
    if not table or not _confirmed(rows, table, shape):
        return []
    side = _quantity_side(rows, table, shape)
    scale_lost = _marks_disagree([fits[0] for fits in (_fitting(row, shape) for row in table) if fits])
    priced: list[tuple[_Row, _Product | None]] = []
    wrapped: dict[int, list[_Row]] = {}
    points = _table_prints_decimal_points(table, shape)
    for row in table:
        fits = _fitting(row, shape) or _out_of_line(row, shape)
        if points and len(fits) > 1:
            fits = [fit for fit in fits if _decimals(fit) == max(map(_decimals, fits))]
        # Two different readings of one row in the same columns is no reading.
        if fits and len({(p.left.value, p.right.value, p.amount.value) for p in fits}) == 1:
            priced.append((row, fits[0]))
        elif _amount_of(row, shape) is not None and any(_LETTER.search(word.text) for word in row.words) and not row.closes:
            priced.append((row, None))
        elif priced and not row.numbers and not row.closes:
            held = wrapped.setdefault(id(priced[-1][0]), [])
            if _wraps_under(priced[-1][0], held[-1] if held else priced[-1][0], row):
                held.append(row)
    texts = [
        _text_of(row, product.used if product else {i for n in row.numbers if n is _amount_of(row, shape) for i in n.words})
        for row, product in priced
    ]
    if all(_letter_words(text) <= 1 for text in texts):
        # Figures only on every row: each row's text is the row printed above it.
        texts = [_with_row_above(rows, row, text) for (row, _), text in zip(priced, texts)]
        wrapped = {}
    elif any(wrapped.values()):
        # Text under the last row could be a footer note, unless this table is seen to wrap its text.
        last_row = priced[-1][0]
        for other in (other for other in rows if (other.page, other.y) > (last_row.page, last_row.y)):
            if other.numbers or other.closes or not _wraps_under(last_row, wrapped.get(id(last_row), [last_row])[-1] if wrapped.get(id(last_row)) else last_row, other):
                break
            wrapped.setdefault(id(last_row), []).append(other)
    lines: list[Line] = []
    for (row, product), described in zip(priced, _describe(texts)):
        evidence = _row_text(row.words)[:500]
        described = {**described, "description": " ".join(
            [described["description"] or ""] + [_row_text(other.words) for other in wrapped.get(id(row), [])]).strip()}
        uom = described["uom"]
        if product is None:
            # Held: the figures did not multiply, so none of them is certain. The evidence keeps the row.
            amount = qty = price = tax = None
        else:
            settled, tax = _settled(product, row.numbers)
            amount = settled
            if _marked_credit(row, amount):
                amount = replace(amount, value=-abs(amount.value))
            factors = {"left": (product.left, product.right), "right": (product.right, product.left)}.get(side or "")
            qty, price = (factors[0].value, factors[1].value) if factors else (None, None)
            if scale_lost:
                amount = price = tax = None
            for number in (product.left, product.right):
                if number.unit:
                    uom = number.unit
        lines.append(
            Line(
                sku=described["sku"],
                gtin=described["gtin"],
                description=_clean_description(described["description"] or ""),
                qty=qty,
                uom=_clean_uom(uom) if uom else None,
                price=price,
                net_amount=amount.value if amount is not None else None,
                tax_amount=tax,
                evidence=evidence or None,
                page=row.page,
            )
        )
    return lines


def _whole_figures(line: dict[str, Any]) -> bool:
    return all(line.get(field) not in (None, "") and "." not in str(line[field]) for field in ("price", "net_amount"))


def _mixed_scale(lines: list[dict[str, Any]]) -> bool:
    """Whole figures beside rows that print three decimals: a scan may have made the decimal point read as a group mark."""
    return any(map(_whole_figures, lines)) and any(
        re.search(r"\.[0-9]{3}$", str(line.get(field) or "")) for line in lines for field in ("price", "net_amount")
    )


def _scale_differs(line: dict[str, Any], other: dict[str, Any]) -> bool:
    """The read of the row's own figures is blank, or the same digits a thousand times smaller."""
    amount, found = _as_decimal(line.get("net_amount")), _as_decimal(other.get("net_amount"))
    return found is None or amount is not None and amount == found * 1000


def _row_multiplies(line: dict[str, Any]) -> bool:
    qty, price, amount = (_as_decimal(line.get(field)) for field in ("qty", "price", "net_amount"))
    if qty is None or price is None or amount is None:
        return False
    return abs(abs(qty * price) - abs(amount)) <= max(Decimal("0.02"), abs(amount) * _ROUNDING_SHARE)


def _as_decimal(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except ArithmeticError:
        return None


def _text_doubtful(line: dict[str, Any]) -> bool:
    """Cells that cannot be what their heading called them: a code with no digit in it, or a barcode as the code;
    a description starting with a code or running on into figures; a unit that is a figure."""
    sku = str(line.get("sku") or "")
    described = str(line.get("description") or "").split()
    return (
        not sku
        or not any(character.isdigit() for character in sku) or (_is_barcode_text(sku) and not line.get("gtin"))
        or any(map(_is_barcode_text, described))
        or any(_PRESENTATION_FORM.search(word) for word in described)
        or bool(described) and bool(_FIGURE_END.search(described[-1]))
        or any(character.isdigit() for character in str(line.get("uom") or ""))
    )


def _is_barcode_text(word: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{8}|[0-9]{12,14}", word)) and _check_digit_ok(word)


def _checked_text(line: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """The row's own text in place of doubtful cells, when it is the same row and it does find a code or a unit."""
    if not _text_doubtful(line):
        return line
    mine, theirs = _as_decimal(line.get("net_amount")), _as_decimal(other.get("net_amount"))
    if mine is not None and theirs is not None and abs(mine) != abs(theirs):
        return line
    sku = str(line.get("sku") or "")
    if not other.get("sku") and (not sku or not line.get("uom") or any(character.isdigit() for character in sku)):
        return line
    return {
        **line,
        **{field: other[field] for field in ("sku", "uom", "description") if other.get(field)},
        "gtin": line.get("gtin") or other.get("gtin"),
    }


def checked_lines(lines: list[dict[str, Any]], words: Iterable[_Word]) -> list[dict[str, Any]]:
    """A heading can name the wrong column: "Ext. Price" read as the price, with no amount left to check it against.
    A row whose figures do not multiply takes them from the rows' own arithmetic, when the page confirms a table;
    otherwise the figure nothing confirms is cleared, so the line rules flag it. Rows that multiply are left alone."""
    figures_fail = not all(_row_multiplies(line) for line in lines)
    mixed_scale = _mixed_scale(lines)
    if not lines or not figures_fail and not mixed_scale and not any(_text_doubtful(line) for line in lines):
        return lines
    found = [line.model_dump(mode="json") for line in read_lines(words)]
    if len(found) > len(lines) and figures_fail:
        return found
    if mixed_scale and len(found) != len(lines):
        return [{**line, "price": None, "net_amount": None} if _whole_figures(line) else line for line in lines]
    if len(found) != len(lines):
        # Not row for row: only rows the same amount identifies may take their text.
        amounts = {abs(_as_decimal(other["net_amount"])): other for other in found if other.get("net_amount") not in (None, "")}
        return [
            _checked_text(line, amounts[abs(_as_decimal(line["net_amount"]))])
            if line.get("net_amount") not in (None, "") and abs(_as_decimal(line["net_amount"])) in amounts else line
            for line in lines
        ]
    merged = []
    for line, other in zip(lines, found):
        if mixed_scale and _whole_figures(line) and _scale_differs(line, other):
            merged.append({**_checked_text(line, other), **{field: other.get(field) for field in ("qty", "price", "net_amount", "tax_amount")}})
        elif _row_multiplies(line):
            merged.append(_checked_text(line, other))
        elif other.get("net_amount") not in (None, "") and other.get("price") not in (None, ""):
            merged.append({**_checked_text(line, other), **{field: other[field] for field in ("qty", "price", "net_amount", "tax_amount")}})
        elif line.get("net_amount") in (None, ""):
            merged.append({**line, "price": None})
        else:
            merged.append(line)
    return merged
