"""Lossless, source-grounded adapters for Docling document tables."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from itertools import groupby
from statistics import fmean, median
from typing import Any

from app.layout_extract import is_packing_page
from app.scan_guard import classify_barcode, lines_reconcile, tax_label_printed


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _page_size(document: Any, page: int) -> list[float] | None:
    pages = _value(document, "pages", {})
    item = pages.get(page) if hasattr(pages, "get") else None
    if item is None and hasattr(pages, "get"):
        item = pages.get(page - 1)
    size = _value(item, "size")
    width, height = _value(size, "width"), _value(size, "height")
    if width is None or height is None:
        return None
    return [float(width), float(height)]


def normalize_bbox(bbox: Any, size: list[float] | None) -> tuple[list[float] | None, str]:
    """Return a top-left bbox without guessing when the page height is absent."""
    if bbox is None:
        return None, "unknown"
    origin = str(_value(bbox, "coord_origin", "unknown"))
    values = [_value(bbox, key) for key in ("l", "t", "r", "b")]
    if any(value is None for value in values):
        return None, origin
    left, top, right, bottom = (float(value) for value in values)
    if "BOTTOMLEFT" in origin.upper():
        if not size:
            return None, origin
        top, bottom = size[1] - top, size[1] - bottom
    return [min(left, right), min(top, bottom), max(left, right), max(top, bottom)], origin


def _word_boxes(parsed_pages: list[Any] | None) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    for page_index, page_item in enumerate(parsed_pages or [], 1):
        parsed = _value(page_item, "parsed_page")
        words = list(_value(parsed, "word_cells", []) or [])
        page_size = _value(page_item, "size")
        size = None
        if page_size is not None:
            size = [float(_value(page_size, "width")), float(_value(page_size, "height"))]
        for word in words:
            rect = _value(word, "rect")
            source_origin = str(_value(rect, "coord_origin", "unknown"))
            xs = [float(_value(rect, f"r_x{index}")) for index in range(4)]
            ys = [float(_value(rect, f"r_y{index}")) for index in range(4)]
            if "BOTTOMLEFT" in source_origin.upper():
                if not size:
                    continue
                ys = [size[1] - value for value in ys]
            text = str(_value(word, "text", "") or "").strip()
            if not text:
                continue
            boxes.append({
                "text": text,
                "page": page_index,
                "box": [min(xs), min(ys), max(xs), max(ys)],
                "size": size,
                "coordinate_system": "top-left",
                "source_coordinate_system": source_origin,
                "geometry": "word",
                "source_type": "TextCell",
                "estimated": False,
                "confidence": float(_value(word, "confidence", 1.0)),
                "from_ocr": bool(_value(word, "from_ocr", False)),
            })
    return boxes


def document_payload(
    document: Any, parsed_pages: list[Any] | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert a DoclingDocument to text, measured blocks, and lossless tables."""
    boxes = _word_boxes(parsed_pages)
    include_blocks = not boxes
    tables: list[dict[str, Any]] = []
    for item, _level in document.iterate_items():
        item_type = type(item).__name__
        text = str(_value(item, "text", "") or "")
        provenance = list(_value(item, "prov", []) or [])
        for prov in provenance:
            page = int(_value(prov, "page_no", 1))
            size = _page_size(document, page)
            box, source_origin = normalize_bbox(_value(prov, "bbox"), size)
            if include_blocks and text and box is not None:
                boxes.append({
                    "text": text,
                    "page": page,
                    "box": box,
                    "size": size,
                    "coordinate_system": "top-left",
                    "source_coordinate_system": source_origin,
                    "geometry": "block",
                    "source_type": item_type,
                    "estimated": False,
                })

        data = _value(item, "data")
        cells = list(_value(data, "table_cells", []) or [])
        if not cells:
            continue
        prov = provenance[0] if provenance else None
        page = int(_value(prov, "page_no", 1))
        size = _page_size(document, page)
        table_box, table_origin = normalize_bbox(_value(prov, "bbox"), size)
        row_count = int(_value(data, "num_rows", 0) or 0)
        column_count = int(_value(data, "num_cols", 0) or 0)
        rows: list[list[str | None]] = [[None] * column_count for _ in range(row_count)]
        public_cells: list[dict[str, Any]] = []
        for cell in cells:
            row = int(_value(cell, "start_row_offset_idx", 0) or 0)
            column = int(_value(cell, "start_col_offset_idx", 0) or 0)
            row_span = int(_value(cell, "row_span", 1) or 1)
            column_span = int(_value(cell, "col_span", 1) or 1)
            cell_text = str(_value(cell, "text", "") or "").strip()
            cell_box, cell_origin = normalize_bbox(_value(cell, "bbox"), size)
            for row_offset in range(row_span):
                for column_offset in range(column_span):
                    target_row, target_column = row + row_offset, column + column_offset
                    if target_row < row_count and target_column < column_count:
                        rows[target_row][target_column] = cell_text or None
            public_cells.append({
                "text": cell_text,
                "row": row,
                "row_span": row_span,
                "column": column,
                "column_span": column_span,
                "is_header": bool(_value(cell, "column_header", False)),
                "box": cell_box,
                "size": size,
                "coordinate_system": "top-left" if cell_box is not None else None,
                "source_coordinate_system": cell_origin,
            })
        tables.append({
            "page": page,
            "rows": rows,
            "cells": public_cells,
            "box": table_box,
            "size": size,
            "coordinate_system": "top-left" if table_box is not None else None,
            "source_coordinate_system": table_origin,
            "source": "docling",
        })
    tables.extend(_tables_from_measured_words(boxes))
    return document.export_to_markdown(), boxes, tables


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE))


def _role(header: Any) -> str | None:
    exact = _exact_header_role(header)
    if exact is not None:
        return exact
    value = _normalized(header)
    packed = value.replace(" ", "")
    if packed in {"sn", "sno"}:
        return "serial"
    if packed in {"barcodepart", "barcodepartnumber", "partcode"}:
        return "part_code"
    checks = (
        ("serial", ("serialnumber", "serialno", "srno", "sr", "serial")),
        ("gtin", ("barcode", "gtin", "ean", "upc")),
        ("description", (
            "productdescription", "itemdescription", "description", "productname",
            "itemname", "servicedescription", "particulars", "name",
        )),
        ("sku", ("productreference", "itemreference", "itemref", "itemcode", "itemnumber",
                 "productcode", "articlecode", "articleno", "sku", "article")),
        ("qty", ("quantity", "qtyinpce", "qtypce", "qty")),
        ("uom", ("unitofmeasure", "uom", "measureunit")),
        ("tax_amount", ("taxamount", "vatamount", "taxamt", "vatamt", "taxvalue", "vatvalue")),
        ("tax_rate", ("taxrate", "vatrate", "taxpercent")),
        ("net_amount", ("taxablevalue", "netamount", "netvalue", "lineamount", "totalprice",
                        "subtotal")),
        ("gross_amount", ("grossamount", "amountincludingtax")),
        ("price", ("sellingprice", "unitprice", "priceperunit", "price")),
    )
    for role, aliases in checks:
        if any(alias in packed for alias in aliases):
            return role
    return None


def _exact_header_role(header: Any) -> str | None:
    packed = _normalized(header).replace(" ", "")
    if re.fullmatch(r"\s*(?:vat|tax)\s*\d+(?:\.\d+)?\s*%\s*", str(header), re.I):
        return "tax_amount"
    # "VAT %" and "Disc %" head a rate, not an amount. They are named so that the figures
    # printed under them keep a column of their own: a heading nothing recognises leaves its
    # column unbounded, and its figures are read as part of the neighbouring cell, which then
    # holds two numbers and parses as neither ("12.50 1 , 234.56" is no amount at all).
    if re.fullmatch(r"\s*(?:vat|tax|gst)\s*%\s*", str(header), re.I):
        return "tax_rate"
    if re.fullmatch(r"\s*disc(?:ount)?\.?\s*%?\s*", str(header), re.I):
        return "discount"
    if re.fullmatch(r"\s*amount\s+(?:AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR)\s*", str(header), re.I):
        return "other_amount"
    aliases = {
        # "Sl No" is as common a serial heading as "S.No"; a scan reads its L as I or 1.
        "serial": {"sn", "sno", "sr", "srno", "serialno", "serialnumber",
                   "serial", "sl", "slno", "si", "sino", "s1no"},
        "part_code": {"barcodepart", "barcodepartnumber"},
        "gtin": {"barcode", "gtin", "ean", "upc"},
        "description": {
            "description", "productdescription", "itemdescription", "productname",
            "itemname", "servicedescription", "particulars", "name",
        },
        "size": {"size"},
        # "Item" alone is the item-code column; "Item Description" still matches
        # description above because the exact table is checked before substrings.
        "sku": {
            "item", "itemref", "itemreference", "itemno", "productreference", "productcode",
            "itemcode", "itemnumber", "articlecode", "articleno", "sku", "article",
        },
        "qty": {"qty", "quantity", "qtyinpce", "qtypce"},
        "uom": {"uom", "unitofmeasure", "measureunit"},
        "price": {"price", "sellingprice", "unitprice", "priceperunit"},
        "net_amount": {
            "amount", "taxablevalue", "netamount", "netvalue", "lineamount", "totalprice",
            "subtotal",
        },
        "tax_rate": {"taxrate", "vatrate", "taxpercent"},
        "tax_amount": {"taxamount", "vatamount", "taxamt", "vatamt", "taxvalue", "vatvalue"},
        "gross_amount": {
            "grossamount", "amountincludingtax", "total", "totalinclvat", "totalincltax",
            "totalincludingvat", "totalincludingtax", "totalinclusiveofvat", "amountinclvat",
        },
        "other_amount": {"otheramount"},
    }
    return next((role for role, values in aliases.items() if packed in values), None)


def _page_slope(words: list[dict[str, Any]]) -> float:
    """Vertical drift per pixel of a tilted scan: the slope that lines printed rows up most sharply.

    Zero unless rows align clearly better when tilted, so a straight page is grouped exactly as before.
    """
    if len(words) < 40:
        return 0.0
    # Some readers give every word of a detected line the whole line's box, so those words
    # share one y and say nothing about where the line sits at each end of the page. Counted
    # once per word, such a line outvotes the very tilt it is evidence of, and a photo held
    # crooked reads as square. Each printed line is therefore counted once.
    centres = [
        (fmean((float(w["box"][0]) + float(w["box"][2])) / 2 for w in run),
         fmean((float(w["box"][1]) + float(w["box"][3])) / 2 for w in run))
        for run in (
            list(group) for _key, group in groupby(
                words, key=lambda word: str(word.get("parent_text") or "") or id(word)
            )
        )
    ]
    if len(centres) < 12:
        return 0.0

    def sharpness(slope: float) -> int:
        bands: dict[int, int] = {}
        for x, y in centres:
            key = round((y - slope * x) / 2)
            bands[key] = bands.get(key, 0) + 1
        return sum(count * count for count in bands.values())

    level = sharpness(0.0)
    # Up to three degrees either way: a page fed through a scanner is out by a fraction of a
    # degree, a page photographed in the hand by a few.
    score, slope = max((sharpness(step / 1000), step / 1000) for step in range(-52, 53))
    return slope if abs(slope) >= 0.003 and score >= level * 1.15 else 0.0


def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    narrower = min(float(left["box"][2]) - float(left["box"][0]), float(right["box"][2]) - float(right["box"][0]))
    shared = min(float(left["box"][2]), float(right["box"][2])) - max(float(left["box"][0]), float(right["box"][0]))
    return narrower > 0 and shared > 0.5 * narrower


def _rows_from_words(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    # Some OCR readers return measured whitespace tokens as well as words.
    # They are not columns and must not widen compound header spans.
    words = [word for word in words if str(word.get("text", "")).strip()]
    if not words:
        return []
    typical = median(max(1.0, float(word["box"][3]) - float(word["box"][1])) for word in words)
    tolerance = max(3.0, typical * 0.6)
    # A tilted scan puts the right end of a printed row half a row lower than its left end; rows are
    # grouped on tilt-corrected centres. Words and their boxes are returned unchanged.
    slope = _page_slope(words)

    def centre_of(word: dict[str, Any]) -> float:
        middle = (float(word["box"][0]) + float(word["box"][2])) / 2
        return (float(word["box"][1]) + float(word["box"][3])) / 2 - slope * middle

    rows: list[list[dict[str, Any]]] = []
    centres: list[float] = []
    for word in sorted(words, key=lambda value: (centre_of(value), float(value["box"][0]))):
        centre = centre_of(word)
        if rows and abs(centre - centres[-1]) <= tolerance:
            rows[-1].append(word)
            centres[-1] = sum(centre_of(item) for item in rows[-1]) / len(rows[-1])
        else:
            rows.append([word])
            centres.append(centre)
    # Two words stacked over the same span cannot share one printed row: closely spaced rows were
    # chained together. Split such a group into rows by height, each word joining the nearest row
    # that has nothing above or below it.
    split: list[list[dict[str, Any]]] = []
    for row in rows:
        if not any(_overlaps(a, b) for i, a in enumerate(row) for b in row[i + 1:]):
            split.append(row)
            continue
        parts: list[tuple[float, list[dict[str, Any]]]] = []
        for word in sorted(row, key=centre_of):
            free = [part for part in parts if not any(_overlaps(word, other) for other in part[1])]
            if free:
                min(free, key=lambda part: abs(centre_of(word) - part[0]))[1].append(word)
            else:
                parts.append((centre_of(word), [word]))
        split.extend(part for _anchor, part in sorted(parts, key=lambda p: median(centre_of(w) for w in p[1])))
    return [sorted(row, key=lambda value: float(value["box"][0])) for row in split]


def _header_roles(row: list[dict[str, Any]]) -> list[tuple[str, float, float]]:
    found: dict[str, tuple[int, float, float]] = {}
    for start in range(len(row)):
        for width in (1, 2, 3):
            cells = row[start : start + width]
            if len(cells) != width:
                continue
            role = _exact_header_role(" ".join(str(cell["text"]) for cell in cells))
            if role is None:
                continue
            # The span is measured across the cells that carry the heading's own letters.
            # A stray "%" from the rate column beside it reads as nothing at all, so
            # "% Net Value" names the same column as "Net Value" and must not be allowed
            # to widen it back over the rate's figures.
            lettered = [cell for cell in cells if re.search(r"[^\W\d_]", str(cell["text"]))]
            if not lettered:
                continue
            if role not in found or width > found[role][0]:
                found[role] = (
                    width,
                    min(float(cell["box"][0]) for cell in lettered),
                    max(float(cell["box"][2]) for cell in lettered),
                )
    spans = [(role, value[1], value[2]) for role, value in found.items()]
    # A compound heading such as Barcode / Part Number owns its whole span;
    # the word Barcode within it is not a second physical column.
    spans = [item for item in spans if not any(
        other[0] != item[0] and other[1] <= item[1] and other[2] >= item[2]
        and other[2] - other[1] > item[2] - item[1]
        for other in spans
    )]
    spans.extend(_total_amount_heading(row, spans))
    spans = [item for item in spans if not any(
        other[0] != item[0] and other[1] <= item[1] and other[2] >= item[2]
        and other[2] - other[1] > item[2] - item[1]
        for other in spans
    )]
    return sorted(
        spans + _unnamed_headings(row, spans),
        key=lambda value: value[1],
    )


def _total_amount_heading(
    row: list[dict[str, Any]], spans: list[tuple[str, float, float]]
) -> list[tuple[str, float, float]]:
    """The column a bare "Total Amount" heads, which is not always the line net.

    Which amount it names cannot be told from the heading alone, so it is read as a rendered
    table is read: the line net only where no other amount or tax column is printed beside
    it, and otherwise a column kept aside until the printed totals decide. What matters here
    is that it has a column at all - without one, its figures join the cell beside it, and
    that cell then holds two numbers and parses as neither.
    """

    named = {role for role, _left, _right in spans}
    role = ("other_amount" if (named & {"net_amount", "tax_amount", "gross_amount"}
                               or any(tax_label_printed(str(cell["text"])) for cell in row))
            else "net_amount")
    if role in named:
        return []
    for start in range(len(row)):
        for width in (2, 1):
            cells = row[start : start + width]
            if len(cells) != width:
                continue
            if _normalized(" ".join(str(cell["text"]) for cell in cells)).replace(" ", "") != "totalamount":
                continue
            left = min(float(cell["box"][0]) for cell in cells)
            right = max(float(cell["box"][2]) for cell in cells)
            if any(left < other_right and other_left < right
                   for found, other_left, other_right in spans if found != "gross_amount"):
                continue
            return [(role, left, right)]
    return []


def _unnamed_headings(
    row: list[dict[str, Any]], spans: list[tuple[str, float, float]]
) -> list[tuple[str, float, float]]:
    """The printed headings this reader cannot name, as columns carrying their printed text.

    A heading nothing recognises still marks where a column begins, and leaving it out makes
    its figures part of the cell beside it: a row headed "... VAT Amount  Total Amount" reads
    its tax cell as "58.25 1 , 234.56" and so reads no tax at all. The text is kept, prefixed
    so it cannot be taken for a role, because which line amount a bare "Total Amount" names is
    decided later from the other headings printed beside it.
    """

    def centre(word: dict[str, Any]) -> float:
        return (float(word["box"][0]) + float(word["box"][2])) / 2

    free = [
        word for word in sorted(row, key=lambda item: float(item["box"][0]))
        if re.search(r"[^\W\d_]", str(word["text"]))
        and not any(left <= centre(word) <= right for _role, left, right in spans)
    ]
    if not free:
        return []
    height = median(max(1.0, float(w["box"][3]) - float(w["box"][1])) for w in free)
    limit = max(6.0, height * 0.8)
    # About the width of one printed space. A corridor wider than that separates columns.
    space = max(4.0, height * 0.45)
    groups: list[list[dict[str, Any]]] = []
    for word in free:
        if groups and float(word["box"][0]) - float(groups[-1][-1]["box"][2]) <= limit:
            groups[-1].append(word)
        else:
            groups.append([word])
    headings = []
    for group in groups:
        left, right = float(group[0]["box"][0]), float(group[-1]["box"][2])
        if any(left < other_right and other_left < right for _role, other_left, other_right in spans):
            continue
        # Columns stand apart by a corridor of white space, so a leftover only a word space
        # from a named heading is the rest of that heading, not a column: the "Amount" of
        # "Total Amount" belongs to the span the word "Total" named, and read as a column of
        # its own it cuts that column's figures in half ("1" and ", 234.56").
        touching = [
            (index, gap)
            for index, (_role, other_left, other_right) in enumerate(spans)
            for gap in (other_left - right if other_left >= right else left - other_right,)
            if 0 <= gap <= space
        ]
        if touching:
            index = min(touching, key=lambda item: item[1])[0]
            role, other_left, other_right = spans[index]
            spans[index] = (role, min(other_left, left), max(other_right, right))
            continue
        headings.append(("_" + " ".join(str(word["text"]).strip() for word in group), left, right))
    return headings


def _column_bounds(roles: list[tuple[str, float, float]]) -> list[float]:
    boundaries = []
    for left, right in zip(roles, roles[1:]):
        gap = max(0.0, right[1] - left[2])
        if left[0] == "description":
            # A left-aligned description heading does not mark the end of its
            # wide column. Keep sizes/numbers beside the description, up to
            # the start of the next printed heading, not the gap midpoint.
            boundary = right[1] - min(3.0, gap * 0.08)
        elif left[0] == "sku":
            boundary = left[2] + min(gap / 2, max(4.0, (left[2] - left[1]) * 0.15))
        else:
            boundary = left[2] + gap / 2
        boundaries.append(boundary)
    return [float("-inf"), *boundaries, float("inf")]


def _printed_cell(words: list[dict[str, Any]]) -> str | None:
    """One cell as the page prints it.

    A reader that reports the text line a word came from spaces that line the way the page
    does, so "50ML+CS50+CASE" is kept rather than respaced to "50ML + CS50 + CASE". The
    line is used only where it covers exactly this cell's words and nothing else.
    """

    spaced = " ".join(str(word["text"]).strip() for word in words).strip() or None
    sources = {str(word.get("parent_text") or "") for word in words}
    printed = next(iter(sources)) if len(sources) == 1 else ""
    if spaced and printed and "".join(printed.split()) == "".join(spaced.split()):
        return " ".join(printed.split())
    return spaced


def _mapped_row(row, roles, bounds, expected_serial=None):
    values = []
    for column in range(len(roles)):
        words = [word for word in row if bounds[column] <= (
            (float(word["box"][0]) + float(word["box"][2])) / 2
        ) < bounds[column + 1]]
        values.append(_printed_cell(words))
    # A right-aligned barcode can start left of its own heading and so centre
    # just inside a wide description column. A whole 8-14 digit token that
    # reaches into the barcode heading's span belongs to the barcode column.
    for column in range(len(roles) - 1):
        if roles[column][0] != "description" or roles[column + 1][0] != "gtin":
            continue
        moved = [word for word in row
                 if bounds[column] <= (float(word["box"][0]) + float(word["box"][2])) / 2 < bounds[column + 1]
                 and float(word["box"][2]) > roles[column + 1][1]
                 and re.fullmatch(r"[0-9]{8,14}", str(word["text"]).strip())]
        if len(moved) == 1 and not values[column + 1]:
            token = str(moved[0]["text"]).strip()
            kept = [str(word["text"]).strip() for word in row
                    if word is not moved[0]
                    and bounds[column] <= (float(word["box"][0]) + float(word["box"][2])) / 2 < bounds[column + 1]]
            values[column] = " ".join(kept).strip() or None
            values[column + 1] = token
    # A right-aligned quantity can end just past the column boundary and land
    # in the price cell ("7 AED 10.25"). A whole integer that ends left of the
    # price heading, with the price still in that cell, is the quantity.
    for column in range(len(roles) - 1):
        if roles[column][0] != "qty" or roles[column + 1][0] != "price" or values[column]:
            continue
        cell = sorted((word for word in row
                       if bounds[column + 1] <= (float(word["box"][0]) + float(word["box"][2])) / 2 < bounds[column + 2]),
                      key=lambda word: float(word["box"][0]))
        if (len(cell) >= 2 and re.fullmatch(r"[0-9]{1,6}", str(cell[0]["text"]).strip())
                and float(cell[0]["box"][2]) < roles[column + 1][1]
                and any(_decimal(word["text"]) is not None for word in cell[1:])):
            values[column] = str(cell[0]["text"]).strip()
            values[column + 1] = " ".join(str(word["text"]).strip() for word in cell[1:]).strip() or None
    for column, role in enumerate(roles):
        if role[0] == "description" and values[column]:
            # OCR reads a printed size's 0 as O and l as I ("3OML", "10mI").
            fixed = re.sub(r"\b(\d+)O(\d*)(?=ml\b)", r"\g<1>0\2", values[column], flags=re.I)
            values[column] = re.sub(r"(?<=\d)mI\b", "ml", fixed)
    mapped = {role[0]: values[i] for i, role in enumerate(roles)}
    # OCR sometimes merges the printed serial with the adjacent part code.
    # Split only its literal expected prefix, with a token measured across
    # those two columns. Never split a code positioned within its own column.
    if (expected_serial is not None and len(roles) >= 2
            and roles[0][0] == "serial" and roles[1][0] in {"part_code", "gtin"}
            and not mapped.get("serial")):
        crossing = [w for w in row if roles[0][1] - 3 <= w["box"][0] < bounds[1]
                    < w["box"][2] <= bounds[2]]
        if len(crossing) == 1:
            raw = str(crossing[0]["text"]).strip()
            prefix = str(expected_serial)
            rest = raw[len(prefix):] if raw.startswith(prefix) else ""
            if (mapped.get(roles[1][0]) == raw
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_-]{2,79}", rest)):
                values[0], values[1] = prefix, rest
                mapped["serial"], mapped[roles[1][0]] = prefix, rest
                mapped["_repair"] = "split OCR token across serial and part columns; verify identifier"
    return values, mapped


def _measured_invoice_number(rows, size):
    """Find an explicit header label even when a logo shares its baseline."""
    candidates = set()
    for row in rows:
        if size and min(w["box"][1] for w in row) > size[1] / 3:
            continue
        text = " ".join(str(w["text"]) for w in row)
        label = re.search(r"\b(?:tax\s+)?invoice\s+(?:no\.?|number|#)\s*[:\-]", text, re.I)
        if not label:
            continue
        # Credit notes may also name the original invoice. That is not the
        # identity of the current document and cannot prove continuation.
        if re.search(r"\b(?:original|related|reference|previous)\s*$", text[:label.start()], re.I):
            continue
        number = _strict_text_headers(text[label.start():]).get("number")
        if number:
            candidates.add(number)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _tables_from_measured_words(boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover tables from measured words and explicit printed column headings.

    Headerless continuation requires the same invoice number, adjacent page,
    unchanged page size and the next printed serial. No numeric substring of
    a product name is eligible as a quantity or price.
    """
    result = []
    required = {"description", "qty", "price"}
    carry = None
    for page in sorted({int(box.get("page", 1)) for box in boxes}):
        page_words = [box for box in boxes if int(box.get("page", 1)) == page]
        rows = _rows_from_words(page_words)
        if is_packing_page("\n".join(" ".join(str(w["text"]) for w in row) for row in rows)):
            # A packing list repeats the goods without being invoice lines.
            carry = None
            continue
        size = page_words[0].get("size") if page_words else None
        number = _measured_invoice_number(rows, size)
        has_header = any(required.issubset({r[0] for r in _header_roles(row)}) for row in rows)
        inherited = bool(
            not has_header and carry and carry["page"] == page - 1
            and number and number == carry["number"] and size == carry["size"]
            and carry["serial"] is not None
        )
        previous = carry
        carry = None
        index = 0
        typical = median(max(1.0, w["box"][3] - w["box"][1]) for w in page_words) if page_words else 6.0
        while index < len(rows):
            roles = previous["roles"] if inherited else _header_roles(rows[index])
            if not required.issubset({r[0] for r in roles}):
                index += 1
                continue
            bounds = _column_bounds(roles)
            # An unnamed column keeps its printed heading, so that which line amount a bare
            # "Total Amount" names is still decided from the headings printed beside it. A
            # heading that would name a role another column already holds is dropped instead:
            # two columns of one role read as one, and the rightmost figure wins.
            named_roles = {role for role, _left, _right in roles if not role.startswith("_")}
            table_rows = [[
                ("" if _role(role[1:]) in named_roles else role[1:])
                if role.startswith("_") else role
                for role, _left, _right in roles
            ]]
            header_text = _normalized(" ".join(str(w["text"]) for w in rows[index]))
            unit_match = re.search(r"\bqty\s+i?n\s+([a-z0-9]{1,10})\b", header_text)
            fixed_values = {"uom": unit_match.group(1).upper()} if unit_match else {}
            awaiting_serial = previous["serial"] + 1 if inherited else None
            header_page = previous["header_page"] if inherited else page
            inherited = False
            if awaiting_serial is None:
                index += 1
            last_serial = None
            last_y = None
            document_total = False
            row_repairs = {}
            while index < len(rows):
                row = rows[index]
                normal = _normalized(" ".join(str(w["text"]) for w in row))
                if re.match(r"^(?:total(?: qty| quantity| price| in)?|net (?:amount|total)"
                            r"|gross total|discount|amount (?:in|chargeable))\b", normal):
                    document_total = True
                    break
                if re.match(r"^(?:created by|approved by|printed by|payment information|bank name|iban)\b", normal):
                    break
                if required.issubset({r[0] for r in _header_roles(row)}):
                    break
                expected = awaiting_serial if awaiting_serial is not None else (
                    last_serial + 1 if last_serial is not None else None
                )
                values, mapped = _mapped_row(row, roles, bounds, expected)
                identity = any(mapped.get(role) for role in ("sku", "gtin", "part_code", "description"))
                price = _decimal(mapped.get("price"))
                qty = _decimal(mapped.get("qty"))
                amount = _decimal(mapped.get("net_amount"))
                serial_text = str(mapped.get("serial") or "").strip()
                serial = int(serial_text) if re.fullmatch(r"[0-9]{1,6}", serial_text) else None
                y = median((w["box"][1] + w["box"][3]) / 2 for w in row)
                if identity and price is not None and (qty is not None or amount is not None):
                    if awaiting_serial is not None:
                        if serial != awaiting_serial:
                            index += 1
                            continue
                        awaiting_serial = None
                    table_rows.append(values)
                    if mapped.get("_repair"):
                        row_repairs[len(table_rows) - 1] = mapped["_repair"]
                    last_serial, last_y = serial, y
                elif (awaiting_serial is None and len(table_rows) > 1
                      and mapped.get("description") and last_y is not None
                      and 0 <= y - last_y <= max(12.0, typical * 2.4)
                      and not any(_decimal(mapped.get(k)) is not None for k in ("qty", "price", "net_amount", "tax_amount"))):
                    description_index = next(i for i, role in enumerate(roles) if role[0] == "description")
                    current = table_rows[-1][description_index] or ""
                    table_rows[-1][description_index] = current + "\n" + mapped["description"]
                    last_y = y
                index += 1
            if len(table_rows) > 1:
                result.append({
                    "page": page, "rows": table_rows, "cells": [], "box": None,
                    "size": size, "coordinate_system": "top-left",
                    "source": "docling_word_cells", "reconstruction": "measured_column_alignment",
                    "fixed_values": fixed_values, "header_page": header_page,
                    "row_repairs": row_repairs,
                })
                if not document_total:
                    carry = {"page": page, "size": size, "number": number,
                             "roles": roles, "serial": last_serial, "header_page": header_page}
            if index < len(rows):
                index += 1
    return result


def _decimal(value: Any) -> str | None:
    text = str(value if value is not None else "").strip().replace("\u00a0", " ")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = re.sub(r"^(?:AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR)\s+", "", text)
    text = re.sub(r"\s+(?:AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR)$", "", text)
    # OCR may split a thousands separator into its own token: "1 , 105.31".
    text = re.sub(r"(?<=\d)\s*,\s*(?=\d{3}(?:\D|$))", ",", text)
    # OCR may read a thousands comma as a point: "1.234.56" has no other reading than 1234.56.
    if re.fullmatch(r"[-+]?\d{1,3}(?:\.\d{3})+\.\d{2}", text):
        head, _, cents = text.rpartition(".")
        text = head.replace(".", ",") + "." + cents
    match = re.fullmatch(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?: \d{3})+|\d+)(?:\.\d+)?", text)
    if not match:
        return None
    number = match.group(0).replace(",", "").replace(" ", "")
    try:
        parsed = Decimal(number)
    except InvalidOperation:
        return None
    if negative:
        parsed = -abs(parsed)
    return format(parsed, "f")


def _gtin(value: Any) -> str | None:
    raw = str(value or "").strip()
    matches = []
    for match in re.finditer(r"(?<!\d)(?:\d[ -]?){7,17}\d(?!\d)", raw):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) in {8, 12, 13, 14}:
            matches.append(digits)
    distinct = list(dict.fromkeys(matches))
    if len(distinct) == 1:
        return distinct[0]
    return raw or None


def _table_rows(table: dict[str, Any]) -> list[list[str | None]]:
    rows = table.get("rows")
    if isinstance(rows, list):
        return [[None if cell is None else str(cell) for cell in row] for row in rows]
    cells = table.get("cells") or []
    row_count = max((int(cell.get("row", 0)) + int(cell.get("row_span", 1)) for cell in cells), default=0)
    col_count = max((int(cell.get("column", 0)) + int(cell.get("column_span", 1)) for cell in cells), default=0)
    result: list[list[str | None]] = [[None] * col_count for _ in range(row_count)]
    for cell in cells:
        row, col = int(cell.get("row", 0)), int(cell.get("column", 0))
        for row_offset in range(max(1, int(cell.get("row_span", 1)))):
            for col_offset in range(max(1, int(cell.get("column_span", 1)))):
                result[row + row_offset][col + col_offset] = str(cell.get("text") or "") or None
    return result


# "Total Amount (excl. VAT)" over a column is the line net, and "(incl. VAT)" the taxed amount.
# A scan's reader drops or swaps a letter of the abbreviation often enough ("exl.", "exI.") that
# it is matched loosely; what the heading must say is total, amount and excluding or including.
_NET_HEADING = re.compile(
    r"(?i)total\s*amount\s*\(?\s*(?:excl|excluding|exclusive|exc|exl|exi|ex)\b[\s.]*(?:of\s+)?(?:vat|tax)"
)
_GROSS_HEADING = re.compile(
    r"(?i)total\s*amount\s*\(?\s*(?:incl|including|inclusive|inc|ind)\b[\s.]*(?:of\s+)?(?:vat|tax)"
)


def _semantic_row_roles(row: list[str | None]) -> dict[int, str]:
    roles = {index: role for index, cell in enumerate(row) if (role := _role(cell))}
    merged = [
        index
        for index, cell in enumerate(row)
        if all(marker in _normalized(cell).replace(" ", "") for marker in (
            "itemcode", "description",
        ))
        and any(marker in _normalized(cell).replace(" ", "") for marker in (
            "sno", "srno", "serial",
        ))
    ]
    for index, cell in enumerate(row):
        if index in roles:
            continue
        text = str(cell or "")
        if _NET_HEADING.search(text):
            roles[index] = "net_amount"
        elif _GROSS_HEADING.search(text):
            roles[index] = "gross_amount"
    for index, cell in enumerate(row):
        if index not in roles and _normalized(cell).replace(" ", "") == "totalamount":
            # 'Total Amount' is the line net only beside no other amount or tax heading;
            # otherwise it is kept aside until the printed totals decide.
            amounts = {"net_amount", "tax_amount", "gross_amount"} & set(roles.values())
            taxed = any(tax_label_printed(str(other or "")) for other in row)
            roles[index] = "other_amount" if amounts or taxed else "net_amount"
    if merged:
        # TableFormer can merge the first three visible headings into a cell
        # spanning the serial and item-code columns, leaving the immediately
        # following description column blank. Preserve their printed order.
        start = merged[0]
        if start + 2 < len(row):
            roles[start] = "serial"
            roles[start + 1] = "sku"
            roles[start + 2] = "description"
    return roles


def _is_line_header(roles: dict[int, str]) -> bool:
    values = set(roles.values())
    return {"qty", "price"}.issubset(values) and bool(
        values & {"sku", "gtin", "part_code", "description"}
    )


def _description_facts(text: str) -> tuple[str | None, str | None]:
    """Separate explicit row metadata from the wrapped product name."""
    description = []
    skus = set()
    # Docling may flatten a cell's visual line breaks. Explicit metadata
    # labels still delimit values; they are not part of the product name.
    parts = re.split(r"\n|(?=\b(?:SKU|COO|country of origin|HS\s*code)\s*:)", text, flags=re.I)
    for line in parts:
        sku = re.fullmatch(r"\s*SKU\s*:\s*([A-Za-z0-9][A-Za-z0-9./_-]{0,79})\s*", line, re.I)
        if sku:
            skus.add(sku.group(1))
        elif not re.match(r"\s*(?:COO|country of origin|HS\s*code)\s*:", line, re.I):
            description.append(line.strip())
    return " ".join(description).strip() or None, next(iter(skus)) if len(skus) == 1 else None


_HEADER_LABELS = {
    "tax invoice no": "number",
    "tax invoice number": "number",
    "invoice no": "number",
    "invoice number": "number",
    "tax invoice date": "date",
    "invoice date": "date",
    "document date": "date",
    "customer po no": "po",
    "customer po number": "po",
    "purchase order no": "po",
    "purchase order number": "po",
    "po no": "po",
    "po number": "po",
    "lpo": "po",
    "lpo no": "po",
    "lpo number": "po",
    "customer lpo no": "po",
    "customer lpo number": "po",
    "currency": "currency",
}


def _header_label(text: str) -> str | None:
    """Return the canonical header label, tolerating one OCR letter slip."""
    normal = _normalized(text)
    if normal in _HEADER_LABELS:
        return normal
    if len(normal) < 10:
        return None
    from difflib import SequenceMatcher

    close = [label for label in _HEADER_LABELS
             if len(label) == len(normal) and SequenceMatcher(None, normal, label).ratio() >= 0.9]
    return close[0] if len(close) == 1 else None


def _header_tables_from_measured_words(boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild a printed label row and the value row directly beneath it.

    Labels are separated by measured gaps; each value word is assigned to the
    label whose column (midpoints between adjacent labels) contains its centre.
    """
    result = []
    for page in sorted({int(box.get("page", 1)) for box in boxes}):
        rows = _rows_from_words([box for box in boxes if int(box.get("page", 1)) == page])
        for row, below in zip(rows, rows[1:]):
            height = median(max(1.0, w["box"][3] - w["box"][1]) for w in row)
            cells: list[list[dict[str, Any]]] = []
            for word in row:
                if cells and float(word["box"][0]) - float(cells[-1][-1]["box"][2]) <= height * 1.2:
                    cells[-1].append(word)
                else:
                    cells.append([word])
            spans = [(" ".join(str(w["text"]).strip() for w in cell),
                      float(cell[0]["box"][0]), float(cell[-1]["box"][2])) for cell in cells]
            labels = [_header_label(text) for text, _left, _right in spans]
            if len({_HEADER_LABELS[label] for label in labels if label}) < 2:
                continue
            gap = float(below[0]["box"][1]) - max(float(w["box"][3]) for w in row)
            if gap > height * 2.5:
                continue
            edges = [float("-inf"), *[(a[2] + b[1]) / 2 for a, b in zip(spans, spans[1:])], float("inf")]
            values = [" ".join(str(w["text"]).strip() for w in below
                               if edges[i] <= (float(w["box"][0]) + float(w["box"][2])) / 2 < edges[i + 1]) or None
                      for i in range(len(spans))]
            header = [label or text for label, (text, _left, _right) in zip(labels, spans)]
            result.append({"page": page, "rows": [header, values], "cells": [], "box": None,
                           "source": "measured_header", "reconstruction": "measured_label_value_rows"})
    return result


def _date_value(value: Any) -> str | None:
    raw = str(value or "").strip()
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if iso:
        return raw
    day_first = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", raw)
    if day_first:
        day, month, year = map(int, day_first.groups())
        try:
            from datetime import date

            return date(year, month, day).isoformat()
        except ValueError:
            return None
    month_name = re.fullmatch(r"(\d{1,2})[- ]([A-Za-z]{3,9})[- ](\d{4})", raw)
    if month_name:
        from datetime import datetime

        for pattern in ("%d-%b-%Y", "%d-%B-%Y", "%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(raw.title(), pattern).date().isoformat()
            except ValueError:
                continue
    # Ambiguous slash/hyphen values are retained separately in date_printed;
    # they cannot populate an ISO date picker without a locale decision.
    from .layout_extract import _parse_date

    return _parse_date(raw)


def _header_value(field: str, value: Any) -> str | None:
    raw = " ".join(str(value or "").split()).strip()
    if not raw or len(raw) > 80:
        return None
    if field == "date":
        return _date_value(raw)
    if field == "currency":
        return raw.upper() if re.fullmatch(r"[A-Za-z]{3}", raw) else None
    if field in {"number", "po"} and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_-]{0,79}", raw):
        return raw
    return None


def _table_header_values(tables: list[dict[str, Any]]) -> dict[str, str]:
    """Read repeated label/value header tables and reject conflicting values."""
    candidates: dict[str, set[str]] = {field: set() for field in {"number", "date", "date_printed", "po", "currency"}}
    for table in sorted(tables, key=lambda value: int(value.get("page", 1))):
        rows = _table_rows(table)
        for row in rows:
            if not any("document date" in _normalized(cell) for cell in row):
                continue
            visible_dates = {
                parsed
                for cell in row
                for raw_date in re.findall(
                    r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{4}|"
                    r"\d{1,2}[- ][A-Za-z]{3,9}[- ]\d{4})\b",
                    str(cell or ""),
                )
                if (parsed := _date_value(raw_date)) is not None
            }
            if len(visible_dates) == 1:
                candidates["date"].update(visible_dates)
        for row_index, row in enumerate(rows[:-1]):
            mapped = {
                column: _HEADER_LABELS[_normalized(cell)]
                for column, cell in enumerate(row)
                if _normalized(cell) in _HEADER_LABELS
            }
            # A genuine invoice header table carries several independent
            # labels. Requiring two prevents captions or footer prose from
            # becoming header values.
            if len(set(mapped.values())) < 2:
                continue
            values = rows[row_index + 1]
            for column, field in mapped.items():
                if column >= len(values):
                    continue
                if field == "date" and values[column]:
                    candidates["date_printed"].add(str(values[column]).strip())
                parsed = _header_value(field, values[column])
                if parsed is not None:
                    candidates[field].add(parsed)
    return {
        field: next(iter(values))
        for field, values in candidates.items()
        if len(values) == 1
    }


def _strict_text_headers(text: str) -> dict[str, str]:
    patterns = {
        "number": re.compile(
            r"(?im)^\s*(?:tax\s+)?invoice\s+(?:no\.?|number|#)\s*[:\-]\s*"
            r"([A-Za-z0-9][A-Za-z0-9./_-]{0,79})\s*$"
        ),
        "po": re.compile(
            r"(?im)^\s*(?:customer\s+)?(?:purchase\s+order|p\.?\s*o\.?)\s*"
            r"(?:(?:no\.?|number|#)\s*)?[:\-]\s*"
            r"([A-Za-z0-9][A-Za-z0-9./_-]{0,79})\s*$"
        ),
        "date": re.compile(
            r"(?im)^\s*(?:tax\s+)?invoice\s+date\s*[:\-]\s*"
            r"(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4})\s*$"
        ),
        "currency": re.compile(r"(?im)^\s*currency\s*[:\-]\s*([A-Za-z]{3})\s*$"),
    }
    result = {}
    for field, pattern in patterns.items():
        values = {
            parsed
            for match in pattern.finditer(text)
            if (parsed := _header_value(field, match.group(1))) is not None
        }
        if len(values) == 1:
            result[field] = next(iter(values))
    return result


def _fill_edge_headings(tables: list[dict[str, Any]], boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill a native table's empty first or last heading from the words printed beside its others.

    A ruled grid can leave an edge heading outside its cells while the column's data sits inside.
    Only an edge cell over a filled data column is filled, and only with the words on the same
    measured row left of the first (or right of the last) printed heading. Nothing is guessed.
    """
    result = []
    for table in tables:
        rows = _table_rows(table) if table.get("source") == "pdfplumber" else []
        if len(rows) < 2 or not rows[0]:
            result.append(table)
            continue
        header = list(rows[0])
        width = len(header)
        edges = [column for column in (0, width - 1)
                 if header[column] is None and width >= 3
                 and header[1 if column == 0 else width - 2] is not None
                 and any(column < len(row) and str(row[column] or "").strip() for row in rows[1:])]
        known = [" ".join(str(cell).split()) for cell in header if cell is not None and str(cell).strip()]
        page_words = [box for box in boxes or [] if int(box.get("page", 1)) == int(table.get("page", 1))]
        if not edges or not known or not page_words:
            result.append(table)
            continue
        wanted = " ".join(known).split()
        for row in _rows_from_words(page_words):
            texts = [str(word["text"]).strip() for word in row]
            starts = [i for i in range(len(texts) - len(wanted) + 1) if texts[i:i + len(wanted)] == wanted]
            if len(starts) != 1:
                continue
            first, last = starts[0], starts[0] + len(wanted) - 1
            beside = {0: texts[:first], width - 1: texts[last + 1:]}
            for column in edges:
                if beside[column]:
                    header[column] = " ".join(beside[column])
            break
        if header != rows[0]:
            table = {**table, "rows": [header, *rows[1:]]}
        result.append(table)
    return result


_BARE_TOTAL = re.compile(
    r"(?im)^\s*total\s*:?\s+((?:(?:AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR)\s+)?[0-9][0-9, ]*\.[0-9]{2})"
    r"(?:\s+(?:AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR))?\s*$")


def line_columns(text: str, tables: list[dict[str, Any]] | None, boxes: list[dict[str, Any]] | None = None) -> set[str]:
    """The line roles under a printed column heading of the page's line table (empty when none was found)."""
    columns: set[str] = set()
    extract_invoice_from_tables(text, tables, boxes=boxes, columns=columns)
    return columns


_AMOUNT_ROLES = {"net_amount", "gross_amount", "tax_amount", "tax_rate"}


def heading_columns(boxes: list[dict[str, Any]] | None) -> set[str]:
    """The roles of the printed column headings found in the measured words alone (no table needed).

    Words are named with the same _role as table headings. A band is the words whose centres lie within
    0.7 x a heading word's height, from any OCR line; it qualifies with at least two heading words and no
    more number-bearing words than heading words, so a body word is never enough, and with one heading
    word that is not an amount, so a totals band alone is never a heading. Only the topmost
    heading block of each page counts: its first qualifying band and the qualifying bands stacked under it,
    each within a word height of the last (a heading printed on two or three lines). A totals band
    below the lines adds nothing.
    """
    words = [word for word in boxes or [] if str(word.get("text", "")).strip()]
    roles = {id(word): _role(word.get("text")) for word in words}

    def centre(word: dict[str, Any]) -> float:
        return (float(word["box"][1]) + float(word["box"][3])) / 2

    columns: set[str] = set()
    for page in sorted({int(word.get("page", 1)) for word in words}):
        page_words = [word for word in words if int(word.get("page", 1)) == page]
        last: float | None = None
        for word in sorted((w for w in page_words if roles[id(w)]), key=centre):
            height = max(1.0, float(word["box"][3]) - float(word["box"][1]))
            band = [other for other in page_words if abs(centre(other) - centre(word)) <= height * 0.7]
            named = [other for other in band if roles[id(other)]]
            numbers = sum(1 for other in band if any(ch.isdigit() for ch in str(other.get("text", ""))))
            # A totals band (Subtotal / Total) names amounts only: a heading also names what the line is.
            if len(named) < 2 or numbers > len(named) or not {roles[id(o)] for o in named} - _AMOUNT_ROLES:
                continue
            if last is not None and centre(word) - last > height:
                break
            columns |= {roles[id(other)] for other in named}
            last = centre(word)
    return columns


def extract_invoice_from_tables(
    text: str,
    tables: list[dict[str, Any]] | None,
    boxes: list[dict[str, Any]] | None = None,
    columns: set[str] | None = None,
) -> dict[str, Any] | None:
    """Extract only values explicitly represented by table columns or labels."""
    tables = list(tables or [])
    if boxes:
        tables = _fill_edge_headings(tables, boxes)
    has_native_line_tables = any(
        table.get("source") == "pdfplumber"
        and any(_is_line_header(_semantic_row_roles(row)) for row in _table_rows(table))
        for table in tables
    )
    if (
        boxes
        and not has_native_line_tables
        and not any(table.get("source") == "docling_word_cells" for table in tables)
    ):
        tables.extend(_tables_from_measured_words(boxes))
    if boxes and not has_native_line_tables:
        tables.extend(_header_tables_from_measured_words(boxes))
    # A native line table keeps its own header values; label/value rows rebuilt from
    # measured words only fill the header fields it left empty.
    measured_headers = (
        _table_header_values(_header_tables_from_measured_words(boxes))
        if boxes and has_native_line_tables else {}
    )
    if not tables:
        return None
    invoice: dict[str, Any] = {"lines": []}
    total_candidates: dict[str, list[tuple[int, str]]] = {"net": [], "tax": []}
    active_by_source: dict[str, tuple[dict[int, str], int]] = {}
    line_candidates: list[tuple[str, dict[str, Any]]] = []
    # The amount columns of each line table, and the rows under the lines that carry
    # numbers but no item: a layout can print its totals row without any label.
    amount_columns_by_source: dict[str, dict[int, str]] = {}
    totals_rows: list[tuple[str, dict[int, str]]] = []
    for table in sorted(tables, key=lambda value: (
        int(value.get("page", 1)), value.get("source") != "docling_word_cells",
    )):
        table_source = str(table.get("source") or "unknown")
        rows = _table_rows(table)
        fixed_values = {
            key: str(value) for key, value in (table.get("fixed_values") or {}).items()
            if key in {"uom"} and str(value).strip()
        }
        printed_units = {
            match.group(1).upper()
            for row in rows
            for cell in row
            if (match := re.search(
                r"\bqty\s+i?n\s+([a-z0-9]{1,10})\b", _normalized(cell)
            )) is not None
        }
        if len(printed_units) == 1:
            fixed_values.setdefault("uom", next(iter(printed_units)))
        roles: dict[int, str] | None = None
        for row_index, row in enumerate(rows):
            row_roles = _semantic_row_roles(row)
            if _is_line_header(row_roles):
                roles = row_roles
                active_by_source[table_source] = roles, len(row)
                amount_columns_by_source[table_source] = {
                    column: role for column, role in roles.items()
                    if role in _LINE_AMOUNT_ROLES
                }
                if columns is not None:
                    columns.update(row_roles.values())
                continue
            total = _totals_row(row)
            if total is not None:
                field, priority, value = total
                total_candidates[field].append((priority, value))
                continue
            active_roles, active_width = active_by_source.get(table_source, (None, None))
            current = roles or (active_roles if active_width == len(row) else None)
            if not current:
                continue
            if _is_line_header(row_roles):  # repeated header
                continue
            values: dict[str, Any] = {}
            part_code = None
            for column, role in current.items():
                if column >= len(row):
                    continue
                if role not in {
                    "sku", "gtin", "description", "qty", "uom", "price",
                    "net_amount", "tax_amount", "part_code", "other_amount", "gross_amount",
                }:
                    continue
                raw = row[column]
                if role in {
                    "qty", "price", "net_amount", "tax_amount", "other_amount", "gross_amount",
                }:
                    parsed = _decimal(raw)
                    if parsed is not None:
                        if role == "gross_amount":
                            # A bare "Total" beside "Total Incl.Vat" can be either the
                            # line net or the taxed amount. Keep it aside until the
                            # line's own quantity x price proves which column it is;
                            # see _resolve_printed_amount_column.
                            values.setdefault("_amount_columns", {})[column] = parsed
                            continue
                        # A second printed amount per line (e.g. "Amount AED" beside
                        # "Net Amount") is kept aside until the totals prove which
                        # column is the line net; see _take_reconciled_amount.
                        values["_other_amount" if role == "other_amount" else role] = parsed
                elif role == "gtin":
                    parsed = _gtin(raw)
                    gtin, unchecked, _reason = classify_barcode(parsed)
                    if unchecked:
                        # A complete code failing the GS1 check digit is never gtin.
                        values["barcode_unchecked"] = unchecked
                    elif parsed is not None:
                        # A checked code, or unreadable text that guard_lines clears and flags.
                        values[role] = gtin or parsed
                elif role == "part_code":
                    part_code = str(raw or "").strip() or None
                    if part_code and re.fullmatch(r"(?:\d{8}|\d{12,14})", part_code):
                        gtin, unchecked, _reason = classify_barcode(part_code)
                        if gtin:
                            values["gtin"] = gtin
                        else:
                            values["barcode_unchecked"] = unchecked
                elif str(raw or "").strip():
                    values[role] = str(raw).strip()
            if values.get("description"):
                description, printed_sku = _description_facts(values["description"])
                if description:
                    values["description"] = description
                else:
                    values.pop("description", None)
                if printed_sku:
                    if values.get("sku") and values["sku"] != printed_sku:
                        values.pop("sku", None)
                    else:
                        values["sku"] = printed_sku
            if part_code and not re.fullmatch(r"(?:\d{8}|\d{12,14})", part_code):
                # Kept even when the description's printed code already set sku.
                values["part_code"] = part_code
            if part_code and "gtin" not in values and "barcode_unchecked" not in values:
                values.setdefault("sku", part_code)
            identity = any(values.get(key) for key in ("sku", "gtin", "description"))
            if not identity:
                numbers = {
                    column: parsed for column in current
                    if column < len(row) and (parsed := _decimal(row[column])) is not None
                }
                if numbers:
                    totals_rows.append((table_source, numbers))
            priced_quantity = values.get("qty") is not None and values.get("price") is not None
            priced_amount = (
                values.get("price") is not None and values.get("net_amount") is not None
            )
            page = int(table.get("page", 1))
            if any(_normalized(cell) in {"total", "grand total"} for cell in row):
                if values.get("net_amount") is not None:
                    total_candidates["net"].append((70, values["net_amount"]))
                if values.get("tax_amount") is not None:
                    total_candidates["tax"].append((70, values["tax_amount"]))
                continue
            if identity and (priced_quantity or priced_amount):
                for key, value in fixed_values.items():
                    values.setdefault(key, value)
                values["page"] = page
                values["evidence"] = f"table page {values['page']} row {row_index + 1}"
                repair = (table.get("row_repairs") or {}).get(row_index)
                if repair:
                    values["evidence"] += f"; {repair}"
                line_candidates.append((table_source, values))

    measured_total = sum(
        source == "docling_word_cells" for source, _values in line_candidates
    )
    printed_total = _printed_line_count(text)
    measured_matches_printed_total = (
        printed_total is not None and measured_total == printed_total
    )
    for page in sorted({values["page"] for _source, values in line_candidates}):
        measured = [
            values for source, values in line_candidates
            if values["page"] == page and source == "docling_word_cells"
        ]
        structural = [
            values for source, values in line_candidates
            if values["page"] == page and source != "docling_word_cells"
        ]
        measured_complete = bool(measured) and (
            measured_matches_printed_total
            or all(
                any(_lines_equivalent(structural_line, measured_line) for measured_line in measured)
                for structural_line in structural
            )
        )
        if measured_complete:
            # Use real measured words when they cover every structural row;
            # this removes duplicate rows caused by cell spans/split headers.
            for measured_line in measured:
                matches = [line for line in structural if _lines_equivalent(line, measured_line)]
                if len(matches) == 1:
                    # A measured header can miss a column that TableFormer
                    # read. Preserve complementary explicit cell facts from
                    # the same uniquely matched row, never calculated values.
                    # gtin and barcode_unchecked are one barcode slot: a line never holds both.
                    barcode_read = any(measured_line.get(f) is not None for f in ("gtin", "barcode_unchecked"))
                    for field in ("sku", "gtin", "barcode_unchecked", "part_code", "uom",
                                  "net_amount", "tax_amount", "_other_amount", "_amount_columns"):
                        if field in ("gtin", "barcode_unchecked") and barcode_read:
                            continue
                        if measured_line.get(field) is None and matches[0].get(field) is not None:
                            measured_line[field] = matches[0][field]
            invoice["lines"].extend(measured)
        else:
            # Preserve every TableFormer/native structural row. Measured rows
            # may add visible rows it missed, but never replace unmatched rows.
            invoice["lines"].extend(structural)
            invoice["lines"].extend(
                values for values in measured
                if not any(_lines_equivalent(values, existing) for existing in structural)
            )

    _resolve_printed_amount_column(invoice["lines"], columns)
    for field, candidates in total_candidates.items():
        if not candidates:
            continue
        best_priority = max(priority for priority, _value in candidates)
        values = {value for priority, value in candidates if priority == best_priority}
        if len(values) == 1:
            invoice[field] = next(iter(values))

    # Measured line tables stop at the totals block, so read its printed
    # "label  amount" lines only when no table supplied that total.
    for field, labels in (("net", "net amount|net total"), ("tax", "vat amount|tax amount|total vat|total tax")):
        if field in invoice:
            continue
        printed = {
            parsed for match in re.finditer(
                rf"(?im)^\s*(?:{labels})\s*:?\s+((?:AED\s+)?[0-9][0-9, ]*\.[0-9]{{2}})\s*$", text)
            if (parsed := _decimal(match.group(1))) is not None
        }
        if len(printed) == 1:
            invoice[field] = next(iter(printed))
    _take_printed_totals_row(invoice, totals_rows, amount_columns_by_source)
    _take_reconciled_amount(invoice)
    if "net" not in invoice and not tax_label_printed(text):
        # A bare 'Total' is the net only when no tax is printed and the line nets sum to it.
        printed = {parsed for match in _BARE_TOTAL.finditer(text or "")
                   if (parsed := _decimal(match.group(1))) is not None}
        if len(printed) == 1 and lines_reconcile(invoice["lines"], next(iter(printed))):
            invoice["net"] = next(iter(printed))
    headers = _table_header_values(tables)
    text_headers = _strict_text_headers(text)
    from .layout_extract import _extract_printed_date

    printed_date = _extract_printed_date(text.splitlines())
    if printed_date:
        text_headers["date_printed"] = printed_date
    for field in ("number", "date", "date_printed", "po", "currency"):
        if field in headers:
            invoice[field] = headers[field]
        elif field in text_headers:
            invoice[field] = text_headers[field]
        elif field in measured_headers:
            invoice[field] = measured_headers[field]
    buyer_name = _table_buyer_name(text, tables, boxes)
    if buyer_name is not None:
        invoice["buyer_name"] = buyer_name
    return invoice if invoice["lines"] else None


_LINE_AMOUNT_ROLES = {"net_amount", "tax_amount", "gross_amount", "other_amount"}


def _sum(values: list[Any]) -> Decimal:
    return sum((Decimal(str(value)) for value in values), Decimal(0))


def _resolve_printed_amount_column(
    lines: list[dict[str, Any]], columns: set[str] | None = None
) -> None:
    """Take a bare "Total" column as the line net only when the line's own arithmetic proves it.

    Layouts headed "... Price  Total  Vat  Vat Amount  Total Incl.Vat" print the line net
    under a bare "Total", which alone cannot be told from the taxed amount. Nothing is
    calculated: a column is accepted only where every line that prints both shows
    quantity x price, so the tax-inclusive column is left out.
    """
    present = {column for line in lines for column in (line.get("_amount_columns") or {})}
    proven: list[int] = []
    for column in sorted(present):
        checked = 0
        for line in lines:
            value = (line.get("_amount_columns") or {}).get(column)
            qty, price = _decimal(line.get("qty")), _decimal(line.get("price"))
            if value is None or qty is None or price is None:
                continue
            if abs(Decimal(value) - Decimal(qty) * Decimal(price)) > Decimal("0.01"):
                checked = -1
                break
            checked += 1
        if checked > 0:
            proven.append(column)
    for line in lines:
        values = line.pop("_amount_columns", None) or {}
        if len(proven) == 1 and line.get("net_amount") is None and proven[0] in values:
            line["net_amount"] = values[proven[0]]
            line["_net_column"] = proven[0]
    if len(proven) == 1 and columns is not None and any(
        line.get("_net_column") is not None for line in lines
    ):
        columns.add("net_amount")


def _take_printed_totals_row(
    invoice: dict[str, Any],
    totals_rows: list[tuple[str, dict[int, str]]],
    amount_columns: dict[str, dict[int, str]],
) -> None:
    """Read an unlabelled totals row, but only where it equals the lines it totals.

    Some layouts print the totals as bare numbers under the amount columns, with no
    "Net Amount" or "VAT" label to find them by. Such a row is taken only when the
    number under the line-net column is the sum of the line nets, and the number under
    the line-tax column the sum of the line taxes: the printed figure and the lines
    prove each other, so a per-page subtotal cannot match and nothing is calculated.
    """
    lines = invoice.get("lines") or []
    net_column = next(
        (line["_net_column"] for line in lines if line.get("_net_column") is not None), None
    )
    for line in lines:
        line.pop("_net_column", None)
    if not lines or not totals_rows:
        return
    for field, role in (("net", "net_amount"), ("tax", "tax_amount")):
        if invoice.get(field) is not None:
            continue
        printed = [line.get(role) for line in lines]
        if any(value is None for value in printed):
            continue
        total = _sum(printed)
        for source, numbers in totals_rows:
            named = amount_columns.get(source) or {}
            column = next(
                (index for index, value in named.items() if value == role), None
            )
            if column is None and role == "net_amount" and net_column in named:
                # The line net was printed under a bare "Total" column.
                column = net_column
            if column is None or column not in numbers:
                continue
            if Decimal(numbers[column]) == total:
                invoice[field] = numbers[column]
                break


def _take_reconciled_amount(invoice: dict[str, Any]) -> None:
    """Prefer the printed amount column that reconciles when a layout prints two per line.

    Some layouts print a pre-discount "Net Amount" and, after it, the amount actually
    charged for the line ("Amount AED"). The line net is the column whose sum is the
    printed net total and whose line VAT is the rate times the amount. Only then is the
    second column taken; otherwise the reader behaves as before. Nothing is calculated.
    """
    lines = invoice.get("lines") or []
    others = [line.pop("_other_amount", None) for line in lines]
    if not lines or any(other is None for other in others):
        return
    header_net = _decimal(invoice.get("net"))
    if header_net is None or Decimal(header_net) == 0:
        return
    amounts = [Decimal(other) for other in others]
    nets = [Decimal(line["net_amount"]) for line in lines if line.get("net_amount") is not None]
    if sum(amounts) != Decimal(header_net) or sum(nets) == Decimal(header_net):
        return
    taxed = [(Decimal(line["tax_amount"]), amount) for line, amount in zip(lines, amounts)
             if line.get("tax_amount") is not None]
    header_tax = _decimal(invoice.get("tax"))
    if header_tax is not None:
        rate = Decimal(header_tax) / Decimal(header_net)
    elif taxed and sum(amount for _tax, amount in taxed) != 0:
        # The printed VAT total is read later from the page header; until then the
        # lines' own tax and amount columns give the rate the layout applied.
        rate = sum(tax for tax, _amount in taxed) / sum(amount for _tax, amount in taxed)
    else:
        rate = Decimal(0)
    rate = rate.quantize(Decimal("0.0001"))
    if any(abs(tax - amount * rate) > Decimal("0.01") for tax, amount in taxed):
        return
    for line, other in zip(lines, others):
        line["net_amount"] = other


def _lines_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _decimal(left.get("qty")) != _decimal(right.get("qty")):
        return False
    if _decimal(left.get("price")) != _decimal(right.get("price")):
        return False
    for field in ("net_amount", "tax_amount"):
        if left.get(field) is not None and right.get(field) is not None:
            if Decimal(str(left[field])) != Decimal(str(right[field])):
                return False
    for field in ("sku", "gtin", "description"):
        left_value = re.sub(r"[^a-z0-9]+", "", str(left.get(field) or "").casefold())
        right_value = re.sub(r"[^a-z0-9]+", "", str(right.get(field) or "").casefold())
        if left_value and left_value == right_value:
            return True
    return False


def _printed_line_count(text: str) -> int | None:
    values = {
        int(match.group(1))
        for match in re.finditer(
            r"(?i)\btotal\s+lines\s*:?\s*(\d{1,4})\b",
            text,
        )
        if 0 < int(match.group(1)) <= 1000
    }
    return next(iter(values)) if len(values) == 1 else None


def _table_buyer_name(
    text: str,
    tables: list[dict[str, Any]],
    boxes: list[dict[str, Any]] | None,
) -> str | None:
    from app.layout_extract import (
        _clean_party_name,
        _extract_buyer_name,
        _group_all_rows,
        _normalise_words,
        _spatial_buyer_name,
    )

    spatial_candidate = _spatial_buyer_name(
        _group_all_rows(_normalise_words(boxes or []))
    )
    if spatial_candidate is not None:
        return spatial_candidate

    candidates: list[str] = []
    text_candidate = _extract_buyer_name(
        [line.strip() for line in str(text or "").splitlines() if line.strip()]
    )
    if text_candidate is not None:
        candidates.append(text_candidate)
    for table in tables:
        for row in _table_rows(table):
            for index, cell in enumerate(row):
                label = _normalized(cell)
                raw_candidate = None
                if label in {"bill to", "buyer"}:
                    raw_candidate = next(
                        (value for value in row[index + 1 :] if str(value or "").strip()),
                        None,
                    )
                elif label.startswith("bill to "):
                    raw_candidate = re.sub(r"(?i)^\s*bill\s+to\s*:?[ \t]*", "", str(cell))
                elif label.startswith("buyer "):
                    raw_candidate = re.sub(r"(?i)^\s*buyer\s*:?[ \t]*", "", str(cell))
                cleaned = _clean_party_name(raw_candidate)
                if cleaned is not None:
                    candidates.append(cleaned)
    normalized: dict[str, str] = {}
    for candidate in candidates:
        normalized.setdefault(_normalized(candidate), candidate)
    return next(iter(normalized.values())) if len(normalized) == 1 else None


def _totals_row(row: list[str | None]) -> tuple[str, int, str] | None:
    labels = {
        "vat amount": ("tax", 100),
        "tax amount": ("tax", 100),
        "total vat": ("tax", 90),
        "total tax": ("tax", 90),
        "vat amount in aed": ("tax", 10),
        "tax amount in aed": ("tax", 10),
        "net amount": ("net", 100),
        "net total": ("net", 110),
        "taxable total": ("net", 90),
        "total excl": ("net", 90),
        "subtotal": ("net", 80),
    }
    for index, cell in enumerate(row):
        label = _normalized(cell)
        if label not in labels:
            continue
        value = next(
            (
                parsed
                for candidate in reversed(row[index + 1 :])
                if (parsed := _decimal(candidate)) is not None
            ),
            None,
        )
        if value is not None:
            field, priority = labels[label]
            return field, priority, value
    return None
