"""Build the hard-case corpus: synthetic invoices that are hard to read, each with its expected JSON beside it.

    .venv/bin/python scripts/build_hard_corpus.py                 # writes hard-cases/
    .venv/bin/python scripts/build_hard_corpus.py --out DIR

Every document is printed from one spec in CASES below, and its expected JSON is written from the same spec, so
what the page prints and what the reader is expected to return cannot drift apart. tests/test_hard_corpus.py checks
the ground truth against the printed page (the PDF's own text layer, not a reader), checks the sums, and checks that
the committed JSON is what this spec writes. docs/HARD_CORPUS.md lists the cases, what makes each hard, the ground-
truth rules, and the hand check.

All suppliers, buyers, codes and amounts are made up. No real invoice is copied here: real invoices stay out of git.

Ground-truth rules (the expected JSON holds what the invoice prints, in the app's own shape):
- number, po, supplier_name: as printed. supplier_name is the issuing company, never the bill-to party.
- date_printed: the invoice date exactly as printed. date: the same date as ISO 8601, except that a numeric date
  whose two first parts are both 12 or less and differ (05/10/2026) is null: the app keeps it only in date_printed
  and does not guess a locale (app/fine_rules.py parse_printed_date, V-008). Any other numeric date is day-first.
- currency: the ISO code the invoice prints.
- net: the taxable amount before VAT (after any invoice discount). tax: the VAT amount. A tax the invoice does not
  print (no VAT line) is null, never 0.
- lines: one per printed item row. description is the full item text, a wrapped cell joined with single spaces.
  gtin is a printed barcode only when its check digit is right; a barcode that fails it is not a GTIN (null).
  net_amount is the printed line amount. A column the invoice does not print is null.
- Amounts are plain numbers with the decimals the invoice prints: no thousands separators.
"""
import argparse
import json
import shutil
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "hard-cases"
FOOTER = "SYNTHETIC TEST DATA. Not a real supplier invoice."
PAGE_W, PAGE_H = 595.0, 842.0  # A4 in points
MARGIN = 42.0


def gtin(body):
    """A GTIN from its digits without the check digit: the GS1 mod-10 check digit appended."""
    digits = [int(c) for c in reversed(body)]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits))
    return body + str((10 - total % 10) % 10)


def gtin_ok(code):
    return code.isdigit() and len(code) in (8, 12, 13, 14) and gtin(code[:-1]) == code


def bad_gtin(body):
    """A 13-digit barcode whose check digit is wrong by one."""
    good = gtin(body)
    return good[:-1] + str((int(good[-1]) + 1) % 10)


# ---- the cases ----------------------------------------------------------------------------------------------------
# A line: code (printed SKU), barcode (printed, may fail its check), description, uom, qty, price (strings as the
# invoice's decimals), and optionally more: text printed as a row of its own under the item, in the description
# column only (a set's contents). A case: header fields, the printed forms, the columns, and how the totals are
# printed: "grid" draws every cell's borders, "qty_unit" prints the unit inside the quantity cell ("9.00 Units")
# instead of in a column of its own, and "totals_in_table" prints the subtotal, VAT and total as rows of the table.

def L(code, barcode, description, uom, qty, price, more=None):
    line = {"code": code, "barcode": barcode, "description": description, "uom": uom, "qty": qty, "price": price}
    if more:
        line["more"] = more
    return line


def full_description(line):
    return f"{line['description']} {line['more']}" if line.get("more") else line["description"]


STATIONERY = [
    L("DSS-1102", gtin("629110200110"), "Archive box lever arch A4", "PCS", "240", "14.75"),
    L("DSS-1150", gtin("629110200115"), "Copy paper A4 80gsm 5-ream carton", "CTN", "180", "96.50"),
    L("DSS-2210", gtin("629110200221"), "Whiteboard marker assorted 4-pack", "PKT", "1200", "6.25"),
    L("DSS-3075", gtin("629110200307"), "Office chair mesh high back", "PCS", "35", "1,240.00"),
]

CASES = [
    {
        "id": "h01-thousands-separators",
        "hard": "Every amount over 999 prints a thousands separator; the net is 5 digits before the point.",
        "supplier": "Desert Sun Supplies LLC", "address": "Plot 22, Synthetic Industrial Area, Sharjah",
        "buyer": "Retail UAE Head Office", "number": "DSS-INV-88412", "number_label": "Invoice No:",
        "date": "2026-10-12", "date_printed": "12/10/2026", "date_label": "Invoice Date:",
        "po": "70415", "po_label": "PO No:", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "lines": STATIONERY,
    },
    {
        "id": "h02-kwd-three-decimals",
        "hard": "Kuwaiti dinars with three decimals (fils) on every amount, and no VAT line at all (tax is null).",
        "supplier": "Gulf Pearl Trading Co. WLL", "address": "Block 3, Synthetic Street, Shuwaikh, Kuwait",
        "buyer": "Retail Kuwait", "number": "GPT/2026/0917", "number_label": "Invoice No:",
        "date": "2026-10-19", "date_printed": "19/10/2026", "date_label": "Date:",
        "po": "92017", "po_label": "PO Number:", "currency": "KWD", "decimals": 3, "thousands": True,
        "vat": None, "lines": [
            L("GP-0071", gtin("628100700071"), "Rose water spray 150 ml", "PCS", "48", "1.250"),
            L("GP-0102", gtin("628100700102"), "Oud hair mist 50 ml", "PCS", "24", "4.875"),
            L("GP-0230", gtin("628100700230"), "Gift set oud collection", "SET", "12", "18.500"),
        ],
    },
    {
        "id": "h03-month-name-date-lpo",
        "hard": "Tax-invoice labels ('Tax Invoice No.', 'LPO Ref'), and the date printed with a month name.",
        "supplier": "Coral Coast Foods FZE", "address": "Warehouse 9, Synthetic Free Zone, Jebel Ali",
        "buyer": "Retail UAE", "number": "CCF-TI-30551", "number_label": "Tax Invoice No.",
        "date": "2026-10-04", "date_printed": "04-Oct-2026", "date_label": "Date of Supply",
        "po": "LPO-77310", "po_label": "LPO Ref", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "title": "TAX INVOICE", "lines": [
            L("CCF-501", gtin("629220050101"), "Basmati rice premium 5 kg", "BAG", "60", "42.00"),
            L("CCF-512", gtin("629220051207"), "Sunflower oil 1.8 L", "BTL", "120", "11.90"),
            L("CCF-530", gtin("629220053003"), "Chickpeas canned 400 g", "CAN", "480", "2.35"),
        ],
    },
    {
        "id": "h04-day-first-date",
        "hard": "Numeric date 05/10/2026: 5 Oct day-first, 10 May month-first. A reader must not guess: date is "
                "null and the date stays as printed in date_printed.",
        "supplier": "Falcon Ridge Electronics LLC", "address": "Shop 14, Synthetic Mall, Abu Dhabi",
        "buyer": "Retail UAE", "number": "FRE-10093", "number_label": "Invoice #",
        "date": "2026-10-05", "date_printed": "05/10/2026", "date_label": "Date",
        "po": "70522", "po_label": "Order Ref", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "lines": [
            L("FRE-USB3", gtin("629330000301"), "USB-C cable braided 1 m", "PCS", "150", "19.00"),
            L("FRE-PWB10", gtin("629330000110"), "Power bank 10000 mAh", "PCS", "40", "79.00"),
        ],
    },
    {
        "id": "h05-two-page-table",
        "hard": "36 item rows run over two pages; the column header repeats on page 2 and the totals are on page 2.",
        "supplier": "Oasis Home Essentials LLC", "address": "Unit 7, Synthetic Logistics Park, Dubai",
        "buyer": "Retail UAE", "number": "OHE-2026-4471", "number_label": "Invoice No:",
        "date": "2026-10-21", "date_printed": "21 October 2026", "date_label": "Invoice Date:",
        "po": "70630", "po_label": "PO No:", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "rows_per_page": 22, "lines": [
            L(f"OHE-{100 + n * 7}", gtin(f"6294400{100 + n * 7:05d}"), f"{kind} {size}", uom, str(qty), price)
            for n, (kind, size, uom, qty, price) in enumerate(
                (k, s, u, q, p)
                for k, u, p in (("Bath towel cotton", "PCS", "24.50"), ("Hand towel cotton", "PCS", "9.75"),
                                ("Bed sheet set", "SET", "89.00"), ("Pillow microfibre", "PCS", "32.00"),
                                ("Storage basket", "PCS", "18.25"), ("Glass tumbler", "BOX", "21.60"))
                for s, q in (("white", 12), ("grey", 8), ("navy", 6), ("sand", 10), ("olive", 4), ("rose", 2))
            )
        ],
    },
    {
        "id": "h06-wrapped-descriptions",
        "hard": "Long item texts wrap over two or three printed rows inside the description cell.",
        "supplier": "Meridian Medical Supplies LLC", "address": "Office 1203, Synthetic Tower, Dubai",
        "buyer": "Retail UAE Pharmacy", "number": "MMS-55120", "number_label": "Invoice No:",
        "date": "2026-10-15", "date_printed": "15/10/2026", "date_label": "Invoice Date:",
        "po": "70710", "po_label": "PO No:", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "desc_width": 150, "lines": [
            L("MMS-GLV-M", gtin("629550001201"),
              "Nitrile examination gloves powder free medium size blue box of 100", "BOX", "30", "28.00"),
            L("MMS-MSK-3", gtin("629550001218"),
              "Disposable face mask three ply ear loop with nose wire pack of 50", "PKT", "45", "12.50"),
            L("MMS-THR-D", gtin("629550001225"), "Digital thermometer", "PCS", "20", "35.00"),
        ],
    },
    {
        "id": "h07-reordered-columns",
        "hard": "Columns in an unusual order (Qty and Unit first, code after the text, barcode last) and the "
                "invoice number and date in a right-hand box.",
        "supplier": "Northwind Toys Trading LLC", "address": "Synthetic Road 5, Al Quoz, Dubai",
        "buyer": "Retail UAE Toys", "number": "NWT 004512", "number_label": "Invoice Number",
        "date": "2026-10-09", "date_printed": "09 Oct 2026", "date_label": "Invoice Date",
        "po": "70801", "po_label": "Customer PO", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "header_box": "right",
        "columns": ["qty", "uom", "description", "code", "price", "amount", "barcode"], "lines": [
            L("NWT-PZ1000", gtin("629660001000"), "Puzzle 1000 pieces city skyline", "PCS", "36", "45.00"),
            L("NWT-BLK250", gtin("629660002502"), "Building blocks 250 pieces", "PCS", "24", "120.00"),
            L("NWT-CAR-RC", gtin("629660003301"), "Remote control car 1:24", "PCS", "18", "99.50"),
        ],
    },
    {
        "id": "h08-discount-before-vat",
        "hard": "An invoice discount sits between the subtotal and the taxable amount, so the item rows add up to "
                "the subtotal, not to the net.",
        "supplier": "Silverleaf Cosmetics FZ-LLC", "address": "Building 4, Synthetic Media City, Dubai",
        "buyer": "Retail UAE Beauty", "number": "SLC-INV-2209", "number_label": "Invoice No:",
        "date": "2026-10-27", "date_printed": "27/10/2026", "date_label": "Invoice Date:",
        "po": "70915", "po_label": "PO No:", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "discount": "62.50", "lines": [
            L("SLC-LIP-07", gtin("629770000707"), "Matte lipstick shade 07", "PCS", "50", "22.50"),
            L("SLC-FND-30", gtin("629770003005"), "Liquid foundation 30 ml", "PCS", "25", "55.00"),
        ],
    },
    {
        "id": "h09-decimal-qty-and-price",
        "hard": "Quantities in kilograms with decimals (2.5, 12.75) and unit prices with three decimals in AED.",
        "supplier": "Blue Harbour Seafood LLC", "address": "Stall 31, Synthetic Fish Market, Deira",
        "buyer": "Retail UAE Fresh", "number": "BHS-77031", "number_label": "Invoice No:",
        "date": "2026-10-30", "date_printed": "30/10/2026", "date_label": "Invoice Date:",
        "po": "71002", "po_label": "PO No:", "currency": "AED", "decimals": 2, "price_decimals": 3,
        "thousands": True, "vat": "5", "lines": [
            L("BHS-HAM", None, "Hammour fillet fresh", "KG", "12.75", "48.125"),
            L("BHS-SHR", None, "Tiger prawns large", "KG", "2.5", "86.400"),
            L("BHS-SAL", None, "Salmon side Norwegian", "KG", "7.2", "64.375"),
        ],
    },
    {
        "id": "h10-bad-barcode",
        "hard": "One printed barcode fails its GTIN check digit, so it is not a GTIN: that line's gtin is null.",
        "supplier": "Lumena Beauty Trading LLC", "address": "Synthetic Business Bay, Dubai",
        "buyer": "Retail UAE", "number": "LBT-2026-0311", "number_label": "Invoice No:",
        "date": "2026-10-08", "date_printed": "08/10/2026", "date_label": "Invoice Date:",
        "po": "70003", "po_label": "PO No:", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "lines": [
            L("LUM-SER30", "00012345678905", "Lumena serum 30 ml", "EA", "8", "60.00"),
            L("LUM-TNR20", bad_gtin("629880002001"), "Lumena toner 200 ml", "EA", "12", "34.00"),
        ],
    },
    {
        "id": "h11-description-only",
        "hard": "No item code, no barcode and no unit column; no PO on the invoice. Lines are known only by text.",
        "supplier": "Palm Grove Cleaning Services LLC", "address": "Synthetic Street 12, Al Barsha, Dubai",
        "buyer": "Retail UAE Facilities", "number": "PGC-1187", "number_label": "Invoice No:",
        "date": "2026-10-31", "date_printed": "31 Oct 2026", "date_label": "Invoice Date:",
        "po": None, "currency": "AED", "decimals": 2, "thousands": True, "vat": "5",
        "columns": ["description", "qty", "price", "amount"], "lines": [
            L(None, None, "Deep cleaning store floor October", None, "1", "2,400.00"),
            L(None, None, "Window cleaning external", None, "2", "650.00"),
            L(None, None, "Waste removal extra trips", None, "6", "85.00"),
        ],
    },
    {
        "id": "h12-buyer-printed-first",
        "hard": "The bill-to block is printed above and larger than the supplier, which only appears in a "
                "'From' block and the footer; the reader must not take the buyer as the supplier.",
        "supplier": "Amber Line Logistics LLC", "address": "Synthetic Cargo Village, Dubai",
        "buyer": "Retail UAE Distribution Centre", "number": "ALL-90215", "number_label": "Invoice No:",
        "date": "2026-10-18", "date_printed": "18/10/2026", "date_label": "Invoice Date:",
        "po": "71150", "po_label": "PO No:", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "buyer_first": True, "lines": [
            L("ALL-PAL-EU", None, "Euro pallet wooden", "PCS", "200", "38.00"),
            L("ALL-STR-50", None, "Stretch film 500 mm roll", "ROL", "60", "27.50"),
        ],
    },
    {
        "id": "h13-bordered-grid",
        "hard": "Every cell is boxed by ruled borders, the unit is printed inside the quantity cell ('9.00 Units'), "
                "and the subtotal, VAT and total are rows of the same table, under the amount column.",
        "supplier": "Saffron Leaf Beauty Trading LLC", "address": "Warehouse 3, Synthetic Industrial City, Ajman",
        "buyer": "Retail UAE Beauty", "number": "SLB-2026-0732", "number_label": "Invoice No:",
        "date": "2026-10-22", "date_printed": "22/10/2026", "date_label": "Invoice Date:",
        "po": "71204", "po_label": "PO No:", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "grid": True, "qty_unit": True, "totals_in_table": True,
        "columns": ["code", "description", "qty", "price", "amount"], "lines": [
            L("SLB-BL-200", None, "Body lotion cocoa 200 ml", "Units", "9.00", "18.50"),
            L("SLB-HC-075", None, "Hand cream almond 75 ml", "Units", "24.00", "12.25"),
            L("SLB-SG-250", None, "Shower gel citrus 250 ml", "Units", "12.00", "15.75"),
            L("SLB-FM-050", None, "Face mask clay 50 g", "Units", "6.00", "32.00"),
        ],
    },
    {
        "id": "h14-continuation-rows",
        "hard": "A gift set prints its contents as a row of its own under the item, in the description column "
                "only, with no code, quantity or amount on it; the subtotal is a row of the table.",
        "supplier": "Jasmine Bay Cosmetics LLC", "address": "Unit 18, Synthetic Trade Centre, Sharjah",
        "buyer": "Retail UAE Beauty", "number": "JBC-INV-41087", "number_label": "Invoice No:",
        "date": "2026-10-26", "date_printed": "26 Oct 2026", "date_label": "Invoice Date:",
        "po": "71230", "po_label": "PO No:", "currency": "AED", "decimals": 2, "thousands": True,
        "vat": "5", "totals_in_table": True, "lines": [
            L("JBC-GS-SPA", gtin("629990010001"), "Spa gift set", "SET", "10", "145.00",
              more="Shea Butter, Lulur Scrub, Oil"),
            L("JBC-LB-010", gtin("629990010018"), "Lip balm vanilla", "PCS", "60", "8.50"),
            L("JBC-GS-TRV", gtin("629990010025"), "Travel gift set", "SET", "8", "96.00",
              more="Shampoo, Conditioner, Body Wash"),
            L("JBC-BM-200", gtin("629990010032"), "Body mist rose 200 ml", "PCS", "30", "27.00"),
        ],
    },
]

# Scans: a native case printed, then scanned (rendered, skewed, noised). The expected JSON is the source case's.
SCANS = [
    {"id": "s01-scan-skewed-thousands", "source": "h01-thousands-separators", "format": "png", "dpi": 150,
     "skew": 1.6, "noise": 10, "hard": "h01 scanned at 150 dpi, skewed 1.6 degrees, with sensor noise."},
    {"id": "s02-scan-jpeg-reordered", "source": "h07-reordered-columns", "format": "jpg", "dpi": 170,
     "skew": -2.4, "noise": 8, "jpeg": 35,
     "hard": "h07 skewed the other way and saved as a heavily compressed JPEG (quality 35)."},
    {"id": "s03-image-only-pdf", "source": "h03-month-name-date-lpo", "format": "pdf", "dpi": 200,
     "skew": 0.8, "noise": 6, "hard": "h03 as a scanned PDF: pages are images, the PDF has no text layer."},
    {"id": "s04-faded-photocopy", "source": "h09-decimal-qty-and-price", "format": "jpg", "dpi": 160,
     "skew": 1.0, "noise": 12, "fade": True, "jpeg": 60,
     "hard": "h09 as a faded grey photocopy: low contrast, the decimal points thin."},
    {"id": "s05-two-page-scan-pdf", "source": "h05-two-page-table", "format": "pdf", "dpi": 150,
     "skew": -1.2, "noise": 8, "hard": "h05's two pages as one scanned image-only PDF."},
    {"id": "s06-tiff-day-first", "source": "h04-day-first-date", "format": "tif", "dpi": 200,
     "skew": 0.5, "noise": 5, "hard": "h04 as a greyscale TIFF scan."},
    {"id": "s07-kwd-scan", "source": "h02-kwd-three-decimals", "format": "png", "dpi": 150,
     "skew": 2.0, "noise": 10, "hard": "h02 scanned: three-decimal fils amounts through OCR."},
    {"id": "s08-discount-scan", "source": "h08-discount-before-vat", "format": "png", "dpi": 160,
     "skew": -0.9, "noise": 9, "hard": "h08 scanned: the discount row read through OCR."},
    {"id": "s09-scan-bordered-grid", "source": "h13-bordered-grid", "format": "png", "dpi": 160,
     "skew": 1.1, "noise": 8, "hard": "h13 scanned: OCR reads the ruled cells as separate boxes."},
]


# ---- amounts ------------------------------------------------------------------------------------------------------

def dec(text):
    return Decimal(text.replace(",", ""))


def q(value, places):
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def plain(value, places):
    return f"{q(value, places):.{places}f}"


def printed(value, places, thousands):
    return f"{q(value, places):,.{places}f}" if thousands else plain(value, places)


def plain_qty(text):
    return str(dec(text).normalize()) if "." in text else text


def totals(case):
    """(line amounts, subtotal, discount, net, tax) as Decimals at the case's decimals."""
    places = case["decimals"]
    amounts = [q(dec(line["qty"]) * dec(line["price"]), places) for line in case["lines"]]
    subtotal = sum(amounts, Decimal(0))
    discount = dec(case["discount"]) if case.get("discount") else Decimal(0)
    net = subtotal - discount
    tax = q(net * dec(case["vat"]) / 100, places) if case["vat"] is not None else None
    return amounts, subtotal, discount, net, tax


def price_text(case, price):
    places = case.get("price_decimals", case["decimals"])
    return printed(dec(price), places, case["thousands"])


def ambiguous_date(printed_date):
    """n/n/yyyy with both parts 12 or less and different: no reader may pick the day (the app's V-008 rule)."""
    parts = printed_date.replace("-", "/").replace(".", "/").split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return False
    first, second = int(parts[0]), int(parts[1])
    return first <= 12 and second <= 12 and first != second


def expected(case):
    """The expected JSON for a case: what the invoice prints, in the app's shape (rules in the docstring)."""
    places = case["decimals"]
    amounts, _, _, net, tax = totals(case)
    lines = []
    for line, amount in zip(case["lines"], amounts):
        price_places = case.get("price_decimals", places)
        lines.append({
            "sku": line["code"],
            "gtin": line["barcode"] if line["barcode"] and gtin_ok(line["barcode"]) else None,
            "description": full_description(line),
            "uom": line["uom"],
            "qty": plain_qty(line["qty"]),
            "price": plain(dec(line["price"]), price_places),
            "net_amount": plain(amount, places),
        })
    return {
        "number": case["number"],
        "supplier_name": case["supplier"],
        "date": None if ambiguous_date(case["date_printed"]) else case["date"],
        "date_printed": case["date_printed"],
        "po": case["po"],
        "currency": case["currency"],
        "net": plain(net, places),
        "tax": plain(tax, places) if tax is not None else None,
        "lines": lines,
    }


def printed_tokens(case):
    """Every string the invoice must print for its expected JSON to be right (checked against the PDF text)."""
    places = case["decimals"]
    amounts, subtotal, discount, net, tax = totals(case)
    tokens = [case["supplier"], case["number"], case["date_printed"], case["currency"],
              printed(net, places, case["thousands"])]
    if case["po"]:
        tokens.append(case["po"])
    if tax is not None:
        tokens.append(printed(tax, places, case["thousands"]))
    for line, amount in zip(case["lines"], amounts):
        tokens += [line["qty"], price_text(case, line["price"]), printed(amount, places, case["thousands"])]
        tokens += [v for v in (line["code"], line["barcode"], line["uom"]) if v]
        tokens += full_description(line).split()
    return tokens


# ---- printing -----------------------------------------------------------------------------------------------------

COLUMN_TITLES = {"code": "Item Code", "barcode": "Barcode", "description": "Description", "uom": "Unit",
                 "qty": "Qty", "price": "Unit Price", "amount": "Amount"}
DEFAULT_COLUMNS = ["code", "barcode", "description", "uom", "qty", "price", "amount"]
NUMERIC = {"qty", "price", "amount"}


def column_widths(columns, case):
    """Column widths filling the printable width: the spare goes to the description, or, when the case fixes the
    description's width (to force wrapping), to the other columns."""
    base = {"code": 62, "barcode": 82, "description": case.get("desc_width", 175), "uom": 34,
            "qty": 72 if case.get("qty_unit") else 40, "price": 62, "amount": 70}
    widths = {c: base[c] for c in columns}
    spare = (PAGE_W - 2 * MARGIN) - sum(widths.values())
    if "desc_width" in case:
        others = [c for c in columns if c != "description"]
        for c in others:
            widths[c] += spare / len(others)
    else:
        widths["description"] += spare
    return [widths[c] for c in columns]


def wrap(text, font, size, width):
    """The text cut into rows that fit the cell width, at word breaks."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    words, rows, row = text.split(), [], ""
    for word in words:
        trial = f"{row} {word}".strip()
        if stringWidth(trial, font, size) <= width - 6 or not row:
            row = trial
        else:
            rows.append(row)
            row = word
    rows.append(row)
    return rows


def draw_pdf(case, path):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H), invariant=1)
    c.setTitle(f"Synthetic invoice {case['number']}")
    c.setAuthor("Invoice Studio hard-case corpus")
    places = case["decimals"]
    amounts, subtotal, discount, net, tax = totals(case)
    columns = case.get("columns", DEFAULT_COLUMNS)
    widths = column_widths(columns, case)
    per_page = case.get("rows_per_page", 40)
    chunks = [list(range(i, min(i + per_page, len(case["lines"])))) for i in range(0, len(case["lines"]), per_page)]
    pages = len(chunks)

    def header():
        y = PAGE_H - MARGIN
        title = case.get("title", "INVOICE")
        if case.get("buyer_first"):
            c.setFont("Helvetica-Bold", 18)
            c.drawString(MARGIN, y - 14, f"Bill To: {case['buyer']}")
            c.setFont("Helvetica", 9)
            c.drawString(MARGIN, y - 28, "Delivery: Synthetic Distribution Centre, Dubai")
            c.setFont("Helvetica-Bold", 14)
            c.drawRightString(PAGE_W - MARGIN, y - 14, title)
            y -= 52
            c.setFont("Helvetica", 9)
            c.drawString(MARGIN, y, f"From: {case['supplier']}")
            c.drawString(MARGIN, y - 12, case["address"])
            y -= 34
        else:
            c.setFont("Helvetica-Bold", 16)
            c.drawString(MARGIN, y - 14, case["supplier"])
            c.setFont("Helvetica", 9)
            c.drawString(MARGIN, y - 28, case["address"])
            c.setFont("Helvetica-Bold", 14)
            c.drawRightString(PAGE_W - MARGIN, y - 14, title)
            y -= 52
        fields = [(case["number_label"], case["number"]), (case["date_label"], case["date_printed"])]
        if case["po"]:
            fields.append((case["po_label"], case["po"]))
        if "amount" in columns and case.get("header_box") == "right":
            fields.append(("Currency", case["currency"]))
        if case.get("header_box") == "right":
            box_x, box_w = PAGE_W - MARGIN - 210, 210
            c.rect(box_x, y - 14 * len(fields) - 6, box_w, 14 * len(fields) + 8)
            for n, (label, value) in enumerate(fields):
                c.setFont("Helvetica", 9)
                c.drawString(box_x + 6, y - 10 - 14 * n, label)
                c.setFont("Helvetica-Bold", 9)
                c.drawRightString(box_x + box_w - 6, y - 10 - 14 * n, value)
            if not case.get("buyer_first"):
                c.setFont("Helvetica", 9)
                c.drawString(MARGIN, y - 10, "Bill To:")
                c.drawString(MARGIN, y - 22, case["buyer"])
            y -= 14 * len(fields) + 22
        else:
            if not case.get("buyer_first"):
                c.setFont("Helvetica", 9)
                c.drawString(MARGIN, y, "Bill To:")
                c.drawString(MARGIN, y - 12, case["buyer"])
            for n, (label, value) in enumerate(fields + [("Currency:", case["currency"])]):
                c.setFont("Helvetica", 9)
                c.drawString(PAGE_W - MARGIN - 200, y - 12 * n, label)
                c.drawString(PAGE_W - MARGIN - 100, y - 12 * n, value)
            y -= 12 * (len(fields) + 1) + 14
        return y

    edges = [MARGIN]
    for w in widths:
        edges.append(edges[-1] + w)

    def rule(y):
        """The line under a table row; with a grid, also the cell borders down from the row above it."""
        c.line(MARGIN, y, PAGE_W - MARGIN, y)
        if case.get("grid"):
            for x in edges:
                c.line(x, rule.top, x, y)
        rule.top = y

    def table_header(y):
        c.setFont("Helvetica-Bold", 8.5)
        rule.top = y + 10
        c.line(MARGIN, y + 10, PAGE_W - MARGIN, y + 10)
        x = MARGIN
        for col, w in zip(columns, widths):
            title = COLUMN_TITLES[col]
            if case.get("header_box") == "right" and col == "amount":
                title = f"Amount {case['currency']}"
            if col in NUMERIC:
                c.drawRightString(x + w - 8, y, title)
            else:
                c.drawString(x + 3, y, title)
            x += w
        rule(y - 4)
        return y - 16

    def cell(col, line, amount):
        qty = f"{line['qty']} {line['uom']}" if case.get("qty_unit") else line["qty"]
        return {"code": line["code"], "barcode": line["barcode"], "description": line["description"],
                "uom": line["uom"], "qty": qty, "price": price_text(case, line["price"]),
                "amount": printed(amount, places, case["thousands"])}[col]

    for page_no, chunk in enumerate(chunks, 1):
        y = header() if page_no == 1 else PAGE_H - MARGIN - 20
        if page_no > 1:
            c.setFont("Helvetica", 9)
            c.drawString(MARGIN, y + 6, f"{case['supplier']}  -  Invoice {case['number']} (continued)")
            y -= 18
        y = table_header(y)
        for i in chunk:
            line, amount = case["lines"][i], amounts[i]
            rows_used = 1
            x = MARGIN
            c.setFont("Helvetica", 8.5)
            for col, w in zip(columns, widths):
                value = cell(col, line, amount)
                if value is None:
                    x += w
                    continue
                if col == "description":
                    rows = wrap(value, "Helvetica", 8.5, w)
                    for r, text in enumerate(rows):
                        c.drawString(x + 3, y - 11 * r, text)
                    rows_used = max(rows_used, len(rows))
                elif col in NUMERIC:
                    c.drawRightString(x + w - 8, y, value)
                else:
                    c.drawString(x + 3, y, value)
                x += w
            if line.get("more"):
                if case.get("grid"):
                    rule(y - 11 * rows_used + 7)
                c.drawString(edges[columns.index("description")] + 3, y - 11 * rows_used - 1, line["more"])
                rows_used += 1
            y -= 11 * rows_used + 5
            if case.get("grid"):
                rule(y + 8)
        if not case.get("grid"):
            rule(y + 8)
        if page_no == pages:
            rows = [("Subtotal", printed(subtotal, places, case["thousands"]))]
            if discount:
                rows.append(("Discount", f"-{printed(discount, places, case['thousands'])}"))
                rows.append(("Taxable amount", printed(net, places, case["thousands"])))
            if tax is not None:
                rows.append((f"VAT {case['vat']}%", printed(tax, places, case["thousands"])))
            rows.append((f"Total {case['currency']}", printed(net + (tax or 0), places, case["thousands"])))
            if case.get("totals_in_table"):
                label_x = edges[columns.index("description")] + 3
                for label, value in rows:
                    c.setFont("Helvetica-Bold" if label.startswith("Total") else "Helvetica", 8.5)
                    c.drawString(label_x, y, label)
                    c.drawRightString(edges[-1] - 8, y, value)
                    y -= 16
                    if case.get("grid"):
                        rule(y + 8)
                rows = []
            y -= 8
            for label, value in rows:
                c.setFont("Helvetica-Bold" if label.startswith("Total") else "Helvetica", 9)
                c.drawString(PAGE_W - MARGIN - 200, y, label)
                c.drawRightString(PAGE_W - MARGIN - 3, y, value)
                y -= 14
        c.setFont("Helvetica", 7.5)
        c.drawString(MARGIN, MARGIN - 12, FOOTER if not case.get("buyer_first")
                     else f"{case['supplier']}. {FOOTER}")
        c.drawRightString(PAGE_W - MARGIN, MARGIN - 12, f"Page {page_no} of {pages}")
        c.showPage()
    c.save()


def scan_pages(pdf_path, scan):
    """The PDF's pages as scanned greyscale images: rendered at the scan's dpi, skewed, faded and noised."""
    import numpy as np
    import pypdfium2
    from PIL import Image, ImageFilter

    rng = np.random.default_rng(sum(map(ord, scan["id"])))
    images = []
    pdf = pypdfium2.PdfDocument(str(pdf_path))
    for index in range(len(pdf)):
        image = pdf[index].render(scale=scan["dpi"] / 72).to_pil().convert("L")
        if scan.get("fade"):
            image = image.point(lambda v: 95 + v * 150 // 255)
        image = image.rotate(scan["skew"], resample=Image.BICUBIC, expand=True, fillcolor=255)
        pixels = np.asarray(image, dtype=np.int16) + rng.normal(0, scan["noise"], (image.height, image.width))
        pixels[pixels > 225] = 255  # the scanner clips paper to white; the noise stays on and around the ink
        image = Image.fromarray(np.clip(pixels, 0, 255).astype("uint8"), "L").filter(ImageFilter.GaussianBlur(0.5))
        images.append(image)
    pdf.close()
    return images


def write_scan(scan, source_pdf, out):
    images = scan_pages(source_pdf, scan)
    path = out / f"{scan['id']}.{scan['format']}"
    if scan["format"] == "pdf":
        images[0].save(path, "PDF", resolution=scan["dpi"], save_all=True, append_images=images[1:])
    elif scan["format"] == "jpg":
        images[0].save(path, "JPEG", quality=scan.get("jpeg", 75))
    elif scan["format"] == "tif":
        images[0].save(path, "TIFF", compression="tiff_lzw", dpi=(scan["dpi"], scan["dpi"]))
    else:
        images[0].save(path, "PNG", optimize=True)
    return path


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def build(out):
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    by_id = {case["id"]: case for case in CASES}
    for case in CASES:
        draw_pdf(case, out / f"{case['id']}.pdf")
        write_json(out / f"{case['id']}.json", expected(case))
    for scan in SCANS:
        write_scan(scan, out / f"{scan['source']}.pdf", out)
        write_json(out / f"{scan['id']}.json", expected(by_id[scan["source"]]))
    return sorted(p.name for p in out.iterdir())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    names = build(args.out)
    print(f"{len(CASES)} printed and {len(SCANS)} scanned cases, {len(names)} files in {args.out}")


if __name__ == "__main__":
    sys.exit(main())
