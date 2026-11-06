"""A scan fed in sideways or upside down, whose reader still read the words: its boxes tell, so turn them.

Some readers turn a text line to read it and report where it sat on the page as fed. Rows then run down the page,
or the table runs bottom to top with its columns mirrored. Nothing is guessed: a page turns only when its words say
so, and a sideways page with no evidence of which way up it goes stays as it is and reads no lines."""
from __future__ import annotations

import re
from typing import Any

from .arithmetic_lines import _best_shape, _build_rows, _Row, _table_rows
from .layout_extract import _normalise_words

_SIDEWAYS_SHARE = 0.5
_MIN_WORDS = 8
_MONEY = re.compile(r"\d[.,]\d{2,3}$")
_WORD = re.compile(r"[^\W\d_]{2}")


def _positioned(entry: dict[str, Any]) -> bool:
    box, size = entry.get("box"), entry.get("size")
    return isinstance(box, (list, tuple)) and len(box) == 4 and isinstance(size, (list, tuple)) and len(size) == 2


def _sideways(entries: list[dict[str, Any]]) -> bool:
    long = [entry for entry in entries if len(str(entry.get("text", "")).strip()) > 2]
    tall = [entry for entry in long if entry["box"][3] - entry["box"][1] > (entry["box"][2] - entry["box"][0]) * 1.2]
    return len(long) >= _MIN_WORDS and len(tall) / len(long) >= _SIDEWAYS_SHARE


def _turned(entry: dict[str, Any], quarter_turns: int) -> dict[str, Any]:
    """The box as it sits once the page is turned clockwise this many quarter turns."""
    x0, y0, x1, y1 = entry["box"]
    width, height = entry["size"]
    if quarter_turns == 2:
        box, size = [width - x1, height - y1, width - x0, height - y0], [width, height]
    elif quarter_turns == 1:
        box, size = [height - y1, x0, height - y0, x1], [height, width]
    else:
        box, size = [y0, width - x1, y1, width - x0], [height, width]
    return {**entry, "box": box, "size": size, "geometry": None}


def _closing_rows(rows: list[_Row]) -> tuple[list[_Row], list[_Row]]:
    """The totals printed above and below the table the rows' arithmetic finds."""
    shape = _best_shape(rows)
    table = _table_rows(rows, shape) if shape else []
    if not table:
        return [], []
    closing = [row for row in rows if row.closes and (row.numbers or any(_MONEY.search(word.text) for word in row.words))]
    return [row for row in closing if row.y < table[0].y], [row for row in closing if row.y > table[-1].y]


def _reads_upright(rows: list[_Row]) -> bool:
    above, below = _closing_rows(rows)
    return bool(below) and not above


def _figure_before_its_label(row: _Row) -> bool:
    """A money figure first and words after it: "1,204.50 Amount Due", which is "Amount Due 1,204.50" turned over."""
    words = [word.text.strip() for word in row.words if word.text.strip()]
    return len(words) > 1 and bool(_MONEY.search(words[0])) and bool(_WORD.search(words[1]))


def _reads_upside_down(rows: list[_Row]) -> bool:
    """The amount column left of what multiplies to it, with figures before their labels above the table and none
    below it: amounts may come first on a page that is the right way up, but its totals do not print above it."""
    shape = _best_shape(rows)
    table = _table_rows(rows, shape) if shape else []
    if not table or shape.amount.x1 > shape.left.x0:
        return False
    above = [row for row in rows if row.page == table[0].page and row.y < table[0].y]
    below = [row for row in rows if row.page == table[-1].page and row.y > table[-1].y]
    return any(map(_figure_before_its_label, above)) and not any(map(_figure_before_its_label, below))


def _rows_of(entries: list[dict[str, Any]]) -> list[_Row]:
    return _build_rows(_normalise_words(entries))


def _turn_for(entries: list[dict[str, Any]]) -> int:
    """Quarter turns this page needs; none unless its words prove it."""
    if _sideways(entries):
        fits = [turn for turn in (1, 3) if _reads_upright(_rows_of([_turned(entry, turn) for entry in entries]))]
        return fits[0] if len(fits) == 1 else 0
    return 2 if _reads_upside_down(_rows_of(entries)) else 0


def turn_pages(boxes: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The boxes with every turned page put right; None when no page needed it."""
    pages: dict[Any, list[dict[str, Any]]] = {}
    for entry in boxes:
        if _positioned(entry):
            pages.setdefault(entry.get("page"), []).append(entry)
    turns = {page: _turn_for(entries) for page, entries in pages.items()}
    if not any(turns.values()):
        return None
    return [
        _turned(entry, turns[entry.get("page")]) if _positioned(entry) and turns[entry.get("page")] else entry
        for entry in boxes
    ]
