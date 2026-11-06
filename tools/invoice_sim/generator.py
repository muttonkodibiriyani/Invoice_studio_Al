"""Deterministic synthetic invoices with a known truth.

BLIND RULE. The layouts and label wordings in this file were written from general knowledge of invoices, before anyone
working on it read the reader's label lists or PR #47's diff. Do not tune them from the reader: a generator built from
the reader's own list proves nothing. Add a family or a wording because invoices print it, never because the reader
knows it. Every value here is made up: no real supplier, buyer, code or barcode.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4, landscape, letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

SEED = 20261106
DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# Arabic presentation forms: isolated, final, initial, medial. Two forms: the letter does not join to the next one.
_FORMS = {
    "ء": (0xFE80,), "آ": (0xFE81, 0xFE82), "أ": (0xFE83, 0xFE84), "ؤ": (0xFE85, 0xFE86), "إ": (0xFE87, 0xFE88),
    "ئ": (0xFE89, 0xFE8A, 0xFE8B, 0xFE8C), "ا": (0xFE8D, 0xFE8E), "ب": (0xFE8F, 0xFE90, 0xFE91, 0xFE92),
    "ة": (0xFE93, 0xFE94), "ت": (0xFE95, 0xFE96, 0xFE97, 0xFE98), "ث": (0xFE99, 0xFE9A, 0xFE9B, 0xFE9C),
    "ج": (0xFE9D, 0xFE9E, 0xFE9F, 0xFEA0), "ح": (0xFEA1, 0xFEA2, 0xFEA3, 0xFEA4), "خ": (0xFEA5, 0xFEA6, 0xFEA7, 0xFEA8),
    "د": (0xFEA9, 0xFEAA), "ذ": (0xFEAB, 0xFEAC), "ر": (0xFEAD, 0xFEAE), "ز": (0xFEAF, 0xFEB0),
    "س": (0xFEB1, 0xFEB2, 0xFEB3, 0xFEB4), "ش": (0xFEB5, 0xFEB6, 0xFEB7, 0xFEB8), "ص": (0xFEB9, 0xFEBA, 0xFEBB, 0xFEBC),
    "ض": (0xFEBD, 0xFEBE, 0xFEBF, 0xFEC0), "ط": (0xFEC1, 0xFEC2, 0xFEC3, 0xFEC4), "ظ": (0xFEC5, 0xFEC6, 0xFEC7, 0xFEC8),
    "ع": (0xFEC9, 0xFECA, 0xFECB, 0xFECC), "غ": (0xFECD, 0xFECE, 0xFECF, 0xFED0), "ف": (0xFED1, 0xFED2, 0xFED3, 0xFED4),
    "ق": (0xFED5, 0xFED6, 0xFED7, 0xFED8), "ك": (0xFED9, 0xFEDA, 0xFEDB, 0xFEDC), "ل": (0xFEDD, 0xFEDE, 0xFEDF, 0xFEE0),
    "م": (0xFEE1, 0xFEE2, 0xFEE3, 0xFEE4), "ن": (0xFEE5, 0xFEE6, 0xFEE7, 0xFEE8), "ه": (0xFEE9, 0xFEEA, 0xFEEB, 0xFEEC),
    "و": (0xFEED, 0xFEEE), "ى": (0xFEEF, 0xFEF0), "ي": (0xFEF1, 0xFEF2, 0xFEF3, 0xFEF4),
}
_LAM_ALEF = {"ا": (0xFEFB, 0xFEFC), "أ": (0xFEF7, 0xFEF8), "إ": (0xFEF9, 0xFEFA), "آ": (0xFEF5, 0xFEF6)}


def shape_arabic(text: str) -> str:
    """Join the letters and return them in visual order, as PDF producers without a text engine write them."""
    out, chars, i = [], list(text), 0
    while i < len(chars):
        ch = chars[i]
        forms = _FORMS.get(ch)
        if not forms:
            out.append(ch)
            i += 1
            continue
        prev = chars[i - 1] if i else ""
        joins_prev = prev in _FORMS and len(_FORMS[prev]) == 4
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if ch == "ل" and nxt in _LAM_ALEF:
            out.append(chr(_LAM_ALEF[nxt][1 if joins_prev else 0]))
            i += 2
            continue
        joins_next = len(forms) == 4 and nxt in _FORMS
        if len(forms) == 1:
            code = forms[0]
        elif joins_prev and joins_next:
            code = forms[3]
        elif joins_prev:
            code = forms[1]
        elif joins_next:
            code = forms[2]
        else:
            code = forms[0]
        out.append(chr(code))
        i += 1
    return "".join(reversed(out))


# Wording pools. English first, Arabic in AR_LABELS. Written blind; see the module docstring.
LABELS = {
    "title": ["TAX INVOICE", "INVOICE", "Commercial Invoice", "Sales Invoice", "Bill", "VAT Invoice", "Tax Invoice / Receipt"],
    "number": ["Invoice No.", "Invoice #", "Invoice Number", "Inv Ref", "Bill Number", "Bill No", "Document Ref",
               "Tax Invoice No", "Voucher No", "Serial No", "Our Reference", "Invoice Code", "Folio", "Doc. No."],
    "date": ["Date", "Invoice Date", "Dated", "Issue Date", "Date of Issue", "Billing Date", "Date of Supply", "Doc. Date"],
    "po": ["PO", "P.O. No", "PO Number", "LPO", "LPO No.", "Customer Order", "Your Ref", "Your Order", "Purchase Order",
           "Order Ref.", "Client PO", "Buyer's Order No"],
    "currency": ["Currency", "Cur.", "Ccy", "Curr", "Invoice Currency", "Currency Code"],
    "supplier": ["Supplier", "Vendor", "Seller", "Sold By", "From", "Issued By", "Billed From", "Supplied By"],
    "buyer": ["Bill To", "Sold To", "Customer", "Invoice To", "Client", "Buyer", "Consignee", "Ship To"],
    "net": ["Net", "Net Total", "Sub Total", "Subtotal", "Amount Excl. VAT", "Taxable Amount", "Total Before Tax",
            "Goods Value", "Net Amount", "Value Excluding Tax", "Excl. Tax Total", "Total (ex VAT)", "Net Value",
            "Total Taxable Supplies", "Merchandise Total", "Total Excluding VAT"],
    "tax": ["VAT", "VAT @ {rate}%", "Tax", "Sales Tax", "Output VAT", "Tax Amount", "VAT Amount ({rate}%)", "GST {rate}%",
            "IVA {rate}%", "TVA {rate}%", "Add: VAT {rate}%", "Total VAT", "Tax Total"],
    "gross": ["Total", "Grand Total", "Amount Due", "Invoice Total", "Balance Payable", "Total Incl. VAT", "Total Payable",
              "Net Payable", "Total Amount", "Amount Payable", "Total Due"],
    "sku": ["Item Code", "SKU", "Part No", "Art. No", "Product ID", "Code", "Ref", "Article", "Cat. No", "Stock Code", "Model"],
    "gtin": ["Barcode", "EAN", "GTIN", "UPC", "Bar Code", "EAN13"],
    "description": ["Description", "Item", "Particulars", "Goods", "Details", "Product", "Item Description", "Description of Goods"],
    "uom": ["UOM", "Unit", "Pack", "U/M", "Unit Type", "Packing"],
    "qty": ["Qty", "Quantity", "Units", "Pcs", "Nos", "Qty.", "No. of Units", "Shipped"],
    "price": ["Price", "Unit Price", "Rate", "Cost", "U.P.", "Unit Cost", "Price/Unit", "Unit Rate"],
    "net_amount": ["Amount", "Line Total", "Value", "Ext. Price", "Total", "Net", "Net Amt", "Extended", "Taxable Value"],
    "discount": ["Disc %", "Discount", "Disc.", "Less %"],
    "line_tax": ["VAT %", "Tax %", "VAT Rate"],
}
AR_LABELS = {
    "title": "فاتورة ضريبية", "number": "رقم الفاتورة", "date": "تاريخ الفاتورة", "po": "أمر الشراء", "currency": "العملة",
    "supplier": "المورد", "buyer": "العميل", "net": "المجموع الفرعي", "tax": "ضريبة القيمة المضافة", "gross": "الإجمالي",
    "sku": "الرمز", "gtin": "الباركود", "description": "الوصف", "uom": "الوحدة", "qty": "الكمية", "price": "السعر",
    "net_amount": "المبلغ",
}
AR_NET_ALT = ["الصافي", "المبلغ غير شامل الضريبة", "المجموع قبل الضريبة"]
AR_TAX_ALT = ["قيمة الضريبة", "الضريبة"]
AR_GROSS_ALT = ["المبلغ المستحق", "الإجمالي شامل الضريبة"]
CREDIT_TITLES = ["CREDIT NOTE", "Credit Memo", "Tax Credit Note", "Sales Return Note", "Credit Advice"]
CREDIT_NUMBER = ["Credit Note No", "CN No.", "Credit Memo #", "Return Ref"]

CURRENCIES = {
    "AED": (2, "UAE Dirhams", "AED"), "SAR": (2, "Saudi Riyals", "SR"), "USD": (2, "US Dollars", "$"), "EUR": (2, "Euro", "€"),
    "GBP": (2, "Pounds Sterling", "£"), "QAR": (2, "Qatari Riyals", "QR"), "EGP": (2, "Egyptian Pounds", "EGP"),
    "INR": (2, "Indian Rupees", "Rs."), "KWD": (3, "Kuwaiti Dinars", "KD"), "BHD": (3, "Bahraini Dinars", "BD"),
    "OMR": (3, "Omani Rials", "RO"), "JPY": (0, "Japanese Yen", "JPY"),
}

_NAME_A = ["Quillon", "Marvex", "Tessara", "Bluefen", "Orchard Row", "Halvane", "Zephra", "Corvane", "Sandmere", "Ibex Point",
           "Nimbara", "Kestrow", "Luminet", "Pelago", "Dunmore", "Alcora", "Fennix", "Rivenhall", "Tamarind Gate", "Oakvale"]
_NAME_B = ["Trading", "Foods", "Supplies", "Hardware", "Distribution", "Stationers", "General Trading", "Logistics",
           "Wholesale", "Imports", "Packaging", "Hygiene", "Provisions", "Industries"]
_NAME_C = ["LLC", "FZE", "Ltd", "W.L.L.", "Co.", "FZCO", "L.L.C", "Pvt Ltd", "GmbH", "S.A.", "Est."]
_BUYER = ["Palm Crescent Retail", "Northgate Stores", "Harbour View Markets", "Sable Road Hypermarket", "Copperleaf Mart"]
_AR_SUPPLIERS = ["شركة النخيل للتجارة", "مؤسسة الواحة للتوريدات", "شركة الخليج للأغذية", "دار السلام للمواد"]
_GOODS = ["Copper elbow 15mm", "Lever arch file A4", "Basmati rice 5kg bag", "Dish liquid lemon 750ml", "Nitrile gloves medium",
          "LED panel 36W", "Stretch film 500mm", "Green tea 100 sachets", "Thermal roll 80x80", "Hand towel 2 ply",
          "Olive oil 1L tin", "PVC tape black", "Sunflower seeds salted", "Bottle brush small", "Masking tape 2 inch",
          "Coconut water 330ml", "Cable tie 200mm", "Glass cleaner 500ml", "Pasta penne 400g", "Wall plug 8mm",
          "Tomato paste 800g", "Wheel castor 75mm", "Garbage bag 90L", "Chickpeas 2kg pouch", "Ballpoint pen blue"]
_AR_GOODS = ["أرز بسمتي", "زيت زيتون", "قفازات طبية", "ورق طباعة", "شاي أخضر", "مناديل ورقية", "معجون طماطم"]
_TRAP_GOODS = ["Total Care Shampoo 500ml", "Subtotal Organizer Tray", "VAT Labels Roll", "Net Bag Cotton", "Discount Card Holder",
               "Grand Total Marker Set", "Tax Folder A4", "Amount Book 100 Sheets", "Balance Scale 5kg", "Invoice Book Duplicate",
               "Page Divider Set", "Date Stamp Self Inking", "Currency Counting Tray", "Total Net Cleaner 1L"]
_UOMS = ["PCS", "CTN", "BOX", "KG", "EA", "PKT", "DOZ", "ROLL", "BAG", "L"]
_TERMS = ["Net 30 days", "Net 45", "30 days from invoice", "Cash on delivery", "Payment terms: Net 60"]


def ean13(rng: random.Random, prefix="299") -> str:
    # 2xx is a restricted-circulation GS1 prefix: these cannot collide with a real product's barcode.
    body = prefix + "".join(rng.choice("0123456789") for _ in range(12 - len(prefix)))
    check = (10 - sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body)) % 10) % 10
    return body + str(check)


def sku(rng: random.Random) -> str:
    style = rng.randrange(6)
    letters = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(rng.choice((2, 3))))
    num = rng.randrange(1000, 99999)
    return [f"{letters}-{num}", f"{num}{rng.randrange(10)}", f"{letters}{num}", f"{letters}.{num % 1000:03d}.{rng.choice('ABC')}",
            f"{num}/{rng.randrange(1, 9)}", f"{letters}{rng.randrange(10)}{rng.choice('XYZ')}{num % 100:02d}"][style]


def group(digits: str, sep: str, indian=False) -> str:
    if len(digits) <= 3:
        return digits
    if indian:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        return sep.join(([head] if head else []) + parts + [tail])
    parts = []
    while digits:
        parts.insert(0, digits[-3:])
        digits = digits[:-3]
    return sep.join(parts)


AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def fmt_number(value: Decimal, places: int, style: str, neg: str = "-") -> str:
    q = abs(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    whole, _, frac = f"{q:f}".partition(".")
    thousand, point = {"en": (",", "."), "plain": ("", "."), "eu": (".", ","), "space": (" ", ","), "swiss": ("'", "."),
                       "indian": (",", "."), "space_dot": (" ", "."), "arabic": ("٬", "٫")}[style]
    whole = group(whole, thousand, style == "indian") if thousand else whole
    text = whole + (point + frac if places else "")
    if style == "arabic":
        text = text.translate(AR_DIGITS)
    if value < 0:
        text = {"-": f"-{text}", "paren": f"({text})", "trail": f"{text}-", "cr": f"{text} CR"}[neg]
    return text


def plain(value: Decimal | None, places: int) -> str | None:
    if value is None:
        return None
    return f"{value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):f}"


# Family -> style overrides and invoice traits. Anything not fixed here is drawn at random for each invoice.
FAMILIES: dict[str, dict] = {
    "grid-classic": {"table": "grid", "head": "stacked", "totals": "right"},
    "plain-whitespace": {"table": "plain", "totals": "right"},
    "ruled-zebra": {"table": "zebra"},
    "columns-reordered": {"columns": "shuffled"},
    "amount-first": {"columns": "amount_first"},
    "merged-qty-price": {"columns": "merged_qty_price"},
    "merged-code-desc": {"columns": "merged_code_desc"},
    "merged-qty-uom": {"columns": "merged_qty_uom"},
    "description-only": {"columns": "description_only"},
    "glued-amounts": {"glued": True},
    "decimal-eu": {"numfmt": "eu", "currency_pool": ["EUR"]},
    "decimal-space": {"numfmt": "space", "currency_pool": ["EUR", "EGP"]},
    "decimal-swiss": {"numfmt": "swiss", "currency_pool": ["EUR", "USD"]},
    "decimal-indian": {"numfmt": "indian", "currency_pool": ["INR"], "big": True},
    "decimal-plain": {"numfmt": "plain", "big": True},
    "three-decimals": {"currency_pool": ["KWD", "BHD", "OMR"]},
    "zero-decimals": {"currency_pool": ["JPY"]},
    "currency-forms": {"cur_mode": "varied"},
    "currency-absent": {"cur_mode": "none"},
    "dual-currency": {"dual": True},
    "arabic-bilingual": {"lang": "bi"},
    "arabic-only": {"lang": "ar"},
    "arabic-digits": {"lang": "ar", "numfmt": "arabic", "ar_digits": True},
    "multipage-table": {"pages": True},
    "multipage-carried": {"pages": True, "carry": True},
    "credit-note": {"credit": True},
    "zero-value": {"zero": True},
    "discount-line": {"line_discount": True},
    "discount-document": {"doc_discount": True},
    "missing-net": {"print_net": False},
    "missing-tax": {"print_tax": False},
    "misleading-labels": {"misleading": True},
    "near-numbers": {"near": True, "head": "two_col"},
    "totals-left": {"totals": "left"},
    "totals-strip": {"totals": "strip_above"},
    "totals-inline": {"totals": "inline"},
    "vat-summary": {"totals": "vat_table"},
    "multi-rate-vat": {"multi_rate": True},
    "boxed-header": {"head": "boxes"},
    "inline-header": {"head": "inline"},
    "value-before-label": {"head": "value_first"},
    "buyer-first": {"supplier_pos": "buyer_first"},
    "supplier-footer": {"supplier_pos": "footer"},
    "supplier-labelled": {"supplier_pos": "labelled"},
    "wrapped-subrows": {"wrap": True, "subrows": True},
    "dropped-row-traps": {"traps": True},
    "receipt-narrow": {"receipt": True},
    "landscape-wide": {"page": "landscape", "wide_columns": True},
    "us-dates": {"datefmt": "us", "page": "letter", "currency_pool": ["USD"]},
}

# How a line relates to the master. Share of lines; the truth Item ID follows from the class.
ITEM_CLASSES = [("in_master_code", 50), ("in_master_barcode", 16), ("not_in_master", 10), ("lookalike_code", 4),
                ("ambiguous_code", 6), ("ambiguous_barcode", 4), ("other_supplier_code", 6), ("other_supplier_barcode", 4)]
_DATEFMT = ["dmy_slash", "dmon", "iso", "long", "dmy_dot", "dmy_dash"]


def fmt_date(d: date, how: str) -> str:
    return {"dmy_slash": d.strftime("%d/%m/%Y"), "dmon": d.strftime("%d-%b-%Y"), "iso": d.isoformat(),
            "long": d.strftime("%B ") + f"{d.day}, {d.year}", "dmy_dot": d.strftime("%d.%m.%Y"), "dmy_dash": d.strftime("%d-%m-%Y"),
            "us": d.strftime("%m/%d/%Y")}[how]


@dataclass
class Invoice:
    id: str
    family: str
    truth: dict
    style: dict = field(default_factory=dict)
    supplier_code: str = ""


@dataclass
class Corpus:
    invoices: list[Invoice]
    master: list[dict]
    seed: int


class _Master:
    def __init__(self, rng):
        self.rng, self.rows, self.next = rng, [], 10000
        self.rivals = [f"{a} {b} {c}" for a, b, c in zip(_NAME_A[-5:], _NAME_B[:5], _NAME_C[:5])]

    def add(self, supplier, code, barcode, description, uom):
        self.next += 1
        item_id = f"SIM{self.next}"
        self.rows.append({"item_id": item_id, "supplier": supplier, "supplier_code": code, "barcode": barcode,
                          "description": description, "uom": uom, "pack": self.rng.choice(("1", "6", "12", "24")),
                          "category": self.rng.choice(("Grocery", "Hardware", "Stationery", "Hygiene"))})
        return item_id

    def assign(self, line, supplier):
        """Put the line's code and barcode in the master as its class says, and return the truth for the item."""
        rng = self.rng
        cls = rng.choices([c for c, _ in ITEM_CLASSES], [w for _, w in ITEM_CLASSES])[0]
        if cls == "in_master_barcode" and not line["gtin"]:
            cls = "in_master_code"
        if cls in ("ambiguous_barcode", "other_supplier_barcode") and not line["gtin"]:
            cls = "not_in_master"
        if cls in ("in_master_code", "ambiguous_code", "other_supplier_code", "lookalike_code") and not line["sku"]:
            cls = "not_in_master"
        desc, uom = line["description"], line["uom"]
        item, acceptable = None, []
        if cls == "in_master_code":
            item = self.add(supplier, line["sku"], line["gtin"] or ean13(rng), desc, uom)
        elif cls == "in_master_barcode":
            # The printed code is a supplier code the master has never seen; the barcode is what identifies the item.
            item = self.add(supplier, "", line["gtin"], desc, uom)
        elif cls == "lookalike_code":
            near = line["sku"][:-1] + ("7" if line["sku"][-1] != "7" else "3")
            acceptable = [self.add(supplier, near, ean13(rng), desc, uom)]
        elif cls == "ambiguous_code":
            acceptable = [self.add(supplier, line["sku"], ean13(rng), desc, uom),
                          self.add(supplier, line["sku"], ean13(rng), desc + " new pack", uom)]
        elif cls == "ambiguous_barcode":
            acceptable = [self.add(supplier, sku(rng), line["gtin"], desc, uom),
                          self.add(supplier, sku(rng), line["gtin"], desc + " new pack", uom)]
        elif cls == "other_supplier_code":
            acceptable = [self.add(rng.choice(self.rivals), line["sku"], ean13(rng), "Unrelated " + desc, uom)]
        elif cls == "other_supplier_barcode":
            acceptable = [self.add(rng.choice(self.rivals), sku(rng), line["gtin"], desc, uom)]
        line.update(item_class=cls, item_id=item, item_wrong_if_picked=acceptable)


def _money(rng, dec, big=False):
    top = rng.choice((90, 400, 2500)) * (40 if big else 1)
    if dec == 0:
        return Decimal(rng.randrange(100, top * 100))
    value = Decimal(rng.randrange(25, top * 100)) / 100
    if dec == 3:
        value += Decimal(rng.randrange(0, 10)) / 1000
    return value


def _style(rng: random.Random, family: str) -> dict:
    fam = dict(FAMILIES[family])
    s = {"family": family, "table": rng.choice(("grid", "lines", "plain", "zebra")),
         "head": rng.choice(("stacked", "right", "two_col", "boxes")), "totals": rng.choice(("right", "right", "left", "vat_table")),
         "columns": "standard", "numfmt": rng.choice(("en", "en", "en", "plain")), "neg": rng.choice(("-", "paren", "trail", "cr")),
         "cur_mode": rng.choice(("label", "column", "prefix", "sentence", "suffix", "symbol")), "supplier_pos": "letterhead",
         "font": rng.choice(("Helvetica", "Times-Roman", "Courier", "DejaVu")), "lang": "en", "page": "a4",
         "datefmt": rng.choice(_DATEFMT), "sep": rng.choice((": ", " ", " : ", ":  ")), "print_net": True, "print_tax": True}
    s.update(fam)
    if s["cur_mode"] == "varied":
        s["cur_mode"] = rng.choice(("column", "prefix", "sentence", "suffix", "symbol"))
    if s["lang"] != "en" or s["numfmt"] == "arabic":
        s["font"] = "DejaVu"
    s["labels"] = {k: rng.choice(v) for k, v in LABELS.items()}
    return s


def _lines(rng, s, dec, master, supplier):
    if s.get("pages"):
        n = rng.randrange(48, 95)
    elif s.get("receipt"):
        n = rng.randrange(3, 9)
    else:
        n = rng.randrange(2, 17)
    has_gtin = rng.random() < 0.6 and s["columns"] not in ("description_only",)
    has_sku = s["columns"] != "description_only"
    rate = s["vat_rate"]
    lines = []
    goods = list(_GOODS)
    rng.shuffle(goods)
    for i in range(n):
        if s["lang"] == "ar" and not s.get("ar_digits") and rng.random() < 0.5:
            desc = rng.choice(_AR_GOODS)
        else:
            desc = goods[i % len(goods)] + (f" {rng.randrange(100, 999)}" if i >= len(goods) else "")
        if s.get("traps") and i % 3 == 0:
            desc = rng.choice(_TRAP_GOODS)
        if s.get("wrap"):
            desc += " " + rng.choice(("with protective outer sleeve and retail ready carton for shelf display",
                                      "assorted seasonal colours, supplied flat packed with fixing kit included"))
        uom = rng.choice(_UOMS)
        qty = Decimal(rng.randrange(1, 480))
        if uom in ("KG", "L") and rng.random() < 0.5:
            qty += Decimal(rng.randrange(1, 100)) / 100
        price = Decimal(0) if s.get("zero") else _money(rng, dec, s.get("big"))
        disc = Decimal(rng.choice((0, 5, 7.5, 10, 12.5, 15))) if s.get("line_discount") else Decimal(0)
        foc = s.get("traps") and i % 4 == 1
        if foc:
            price = Decimal(0)
        amount = (qty * price * (100 - disc) / 100).quantize(Decimal(1).scaleb(-dec), rounding=ROUND_HALF_UP)
        line = {"sku": sku(rng) if has_sku else None, "gtin": ean13(rng) if has_gtin else None, "description": desc, "uom": uom,
                "qty": qty, "price": price, "discount": disc, "net_amount": amount,
                "line_rate": Decimal(rng.choice((0, 5))) if s.get("multi_rate") else rate}
        if s.get("subrows"):
            line["subrow"] = rng.choice((f"Lot L{rng.randrange(1000, 9999)} Exp 03/2029", f"Batch {rng.randrange(10000, 99999)}",
                                         f"Serial SN{rng.randrange(100000, 999999)}", "Country of origin: Oman"))
        master.assign(line, supplier)
        lines.append(line)
    return lines


def build_invoice(index: int, family: str, seed: int, master: _Master) -> Invoice:
    rng = random.Random(f"{seed}:{family}:{index}")
    s = _style(rng, family)
    code = s.get("currency_pool") and rng.choice(s["currency_pool"]) or rng.choice(list(CURRENCIES))
    if s["lang"] != "en":
        code = rng.choice(("AED", "SAR", "QAR", "KWD", "OMR", "BHD"))
    dec = CURRENCIES[code][0]
    s["vat_rate"] = Decimal(rng.choice((5, 5, 15, 10, 20, 16)))
    if s.get("zero"):
        s["vat_rate"] = Decimal(rng.choice((0, 5)))
    supplier = f"{rng.choice(_NAME_A)} {rng.choice(_NAME_B)} {rng.choice(_NAME_C)}"
    if s["lang"] == "ar" and rng.random() < 0.5:
        supplier = rng.choice(_AR_SUPPLIERS)
    lines = _lines(rng, s, dec, master, supplier)
    q = Decimal(1).scaleb(-dec)
    subtotal = sum((x["net_amount"] for x in lines), Decimal(0))
    doc_discount = Decimal(0)
    if s.get("doc_discount"):
        doc_discount = (subtotal * Decimal(rng.choice((2, 3, 5, 10))) / 100).quantize(q, rounding=ROUND_HALF_UP)
    net = subtotal - doc_discount
    tax_applies = s["print_tax"] or rng.random() < 0.5 and False
    if s.get("multi_rate"):
        tax = sum((x["net_amount"] * x["line_rate"] / 100 for x in lines), Decimal(0)).quantize(q, rounding=ROUND_HALF_UP)
    else:
        tax = (net * s["vat_rate"] / 100).quantize(q, rounding=ROUND_HALF_UP) if tax_applies else Decimal(0)
    gross = net + tax
    credit = s.get("credit")
    sign = Decimal(-1) if credit else Decimal(1)
    s["credit_positive"] = bool(credit and rng.random() < 0.4)
    issued = date(2026, 1, 1) + timedelta(days=rng.randrange(0, 300))
    if s["datefmt"] == "us":
        issued = issued.replace(day=rng.randrange(13, 29))
    number = rng.choice((f"INV-{rng.randrange(1000, 999999)}", f"{rng.randrange(10000, 9999999)}", f"SI/{issued.year}/{rng.randrange(100, 99999):05d}",
                         f"T{rng.randrange(10, 99)}-{rng.randrange(1000, 9999)}-{rng.choice('ABCD')}", f"{rng.choice('ABXQ')}{rng.randrange(100000, 999999)}"))
    if credit:
        number = f"CN-{rng.randrange(1000, 99999)}"
    po = rng.choice((f"PO-{rng.randrange(1000, 99999)}", f"{rng.randrange(100000, 9999999)}", f"LPO/{rng.randrange(100, 9999)}"))
    s["printed_date"] = fmt_date(issued, s["datefmt"])
    s["buyer"] = rng.choice(_BUYER)
    s["trn"] = f"1{rng.randrange(10**13, 10**14)}3"
    s["other_ref"] = f"{rng.randrange(100000, 999999)}"
    s["delivery_note"] = f"DN-{rng.randrange(1000, 99999)}"
    s["rng_seed"] = rng.randrange(1 << 30)
    if s.get("dual"):
        s["dual_code"] = rng.choice([c for c in ("AED", "SAR", "USD", "EUR") if c != code])
        s["dual_rate"] = Decimal(rng.randrange(25, 490)) / 100
    truth = {
        "number": number, "supplier_name": supplier, "date": issued.isoformat(), "date_printed": s["printed_date"], "po": po,
        "currency": None if s["cur_mode"] == "none" else code, "dec": dec, "net": plain(sign * net, dec),
        "net_printed": s["print_net"], "tax": plain(sign * tax, dec) if s["print_tax"] else None, "tax_printed": s["print_tax"],
        "gross": plain(sign * gross, dec), "document_type": "credit_note" if credit else "invoice", "subtotal": plain(sign * subtotal, dec),
        "lines": [{"sku": x["sku"], "gtin": x["gtin"], "description": x["description"], "uom": x["uom"], "qty": f"{x['qty'].normalize():f}",
                   "price": plain(x["price"], dec), "net_amount": plain(sign * x["net_amount"], dec), "item_class": x["item_class"],
                   "item_id": x["item_id"], "item_wrong_if_picked": x["item_wrong_if_picked"]} for x in lines],
    }
    s["internal_lines"] = lines
    s["totals_values"] = {"net": sign * net, "tax": sign * tax, "gross": sign * gross, "subtotal": sign * subtotal,
                          "doc_discount": -doc_discount, "dec": dec, "sign": sign, "code": code}
    return Invoice(f"{family}-{index:02d}", family, truth, s, code)


def build_corpus(seed: int = SEED, per_family: int = 5) -> Corpus:
    master = _Master(random.Random(f"{seed}:master"))
    invoices = [build_invoice(i, fam, seed, master) for fam in FAMILIES for i in range(per_family)]
    return Corpus(invoices, master.rows, seed)


# Rendering.
_registered = False


def _fonts():
    global _registered
    if not _registered:
        pdfmetrics.registerFont(TTFont("DejaVu", DEJAVU))
        _registered = True


class _Doc:
    def __init__(self, inv: Invoice, path: Path):
        _fonts()
        self.inv, self.s, self.rng = inv, inv.style, random.Random(inv.style["rng_seed"])
        size = {"a4": A4, "letter": letter, "landscape": landscape(A4)}[self.s["page"]]
        self.lines = self.s["internal_lines"]
        if self.s.get("receipt"):
            size = (226, max(520, 340 + 34 * len(self.lines)))
        self.c = canvas.Canvas(str(path), pagesize=size, pageCompression=1)
        self.w, self.h = size
        self.m = 14 if self.s.get("receipt") else 42
        self.y = self.h - self.m
        self.font = self.s["font"]
        self.page = 1

    def text(self, x, y, value, size=9, align="l", font=None):
        value = str(value)
        font = font or self.font
        if any("؀" <= ch <= "ۿ" or "ﹰ" <= ch <= "﻿" for ch in value) or "€" in value and font == "DejaVu":
            font = "DejaVu"
        self.c.setFont(font, size)
        {"l": self.c.drawString, "r": self.c.drawRightString, "c": self.c.drawCentredString}[align](x, y, value)

    def width(self, value, size=9):
        return pdfmetrics.stringWidth(str(value), self.font, size)

    def label(self, key, fallback=None):
        s = self.s
        word = fallback or s["labels"][key]
        if "{rate}" in word:
            word = word.format(rate=f"{s['vat_rate'].normalize():f}")
        if s.get("credit") and key == "number":
            word = self.rng.choice(CREDIT_NUMBER)
        if s["lang"] == "ar":
            ar = AR_LABELS.get(key)
            if key == "net" and self.rng.random() < .5:
                ar = self.rng.choice(AR_NET_ALT)
            if key == "tax" and self.rng.random() < .5:
                ar = self.rng.choice(AR_TAX_ALT)
            if key == "gross" and self.rng.random() < .5:
                ar = self.rng.choice(AR_GROSS_ALT)
            return shape_arabic(ar) if ar else word
        if s["lang"] == "bi" and key in AR_LABELS:
            return f"{word} {shape_arabic(AR_LABELS[key])}"
        return word

    def num(self, value, *, total=False):
        s, tv = self.s, self.s["totals_values"]
        text = fmt_number(value, tv["dec"], s["numfmt"], s["neg"])
        if not total:
            return text
        code = tv["code"]
        mode = s["cur_mode"]
        if mode == "prefix":
            return f"{code} {text}"
        if mode == "suffix":
            return f"{text} {code}"
        if mode == "symbol":
            return f"{CURRENCIES[code][2]}{text}"
        return text

    def shown(self, value):
        """The value printed for a total: credit notes are sometimes printed without a minus."""
        return abs(value) if self.s.get("credit_positive") else value

    def footer(self):
        s = self.s
        if s.get("pages"):
            self.text(self.w - self.m, 22, f"Page {self.page}", 7, "r")
        if s["supplier_pos"] == "footer":
            self.text(self.w / 2, 34, f"{self.inv.truth['supplier_name']} | P.O. Box {self.rng.randrange(100, 9999)} | TRN {s['trn']}", 7.5, "c")

    def new_page(self):
        self.footer()
        self.c.showPage()
        self.page += 1
        self.y = self.h - self.m

    # Header.
    def head_fields(self):
        t, s = self.inv.truth, self.s
        fields = [("number", t["number"]), ("date", s["printed_date"]), ("po", t["po"])]
        if s["cur_mode"] == "label":
            fields.append(("currency", s["totals_values"]["code"]))
        if s.get("near"):
            fields[1:1] = [(None, ("Order No", s["other_ref"])), (None, ("Delivery Note No", s["delivery_note"]))]
            fields.append((None, ("Due Date", fmt_date(date.fromisoformat(t["date"]) + timedelta(days=30), s["datefmt"]))))
            fields.append((None, ("Order Date", fmt_date(date.fromisoformat(t["date"]) - timedelta(days=9), s["datefmt"]))))
        if s.get("credit"):
            fields.append((None, ("Against Invoice", f"INV-{self.rng.randrange(1000, 99999)}")))
        return [(self.label(k), v) if k else v for k, v in fields]

    def header(self):
        s, t = self.s, self.inv.truth
        title = self.rng.choice(CREDIT_TITLES) if s.get("credit") else s["labels"]["title"]
        if s["lang"] != "en":
            title = shape_arabic(AR_LABELS["title"]) + ("" if s["lang"] == "ar" else f" {title}")
        receipt = s.get("receipt")
        name_big = 10 if receipt else 15
        supplier = shape_arabic(t["supplier_name"]) if any("؀" <= c <= "ۿ" for c in t["supplier_name"]) else t["supplier_name"]
        buyer_block = [f"{self.label('buyer')}{s['sep'] if s['lang'] == 'en' else ' '}{s['buyer']}", f"Al Quoz Block {self.rng.randrange(1, 9)}, Unit {self.rng.randrange(1, 99)}"]
        address = f"P.O. Box {self.rng.randrange(100, 9999)}  Tel +000 555 {self.rng.randrange(1000, 9999)}"
        taxid = f"{self.rng.choice(('TRN', 'VAT No', 'Tax Reg. No', 'VAT Reg', 'GSTIN'))}{s['sep']}{s['trn']}"
        if s["supplier_pos"] == "buyer_first":
            for row in buyer_block:
                self.text(self.m, self.y, row, 9)
                self.y -= 12
            self.y -= 6
        if s["supplier_pos"] == "labelled":
            self.text(self.m, self.y, f"{s['labels']['supplier']}{s['sep']}{supplier}", 10)
            self.y -= 13
        elif s["supplier_pos"] != "footer":
            self.text(self.m, self.y, supplier, name_big)
            self.y -= name_big + 3
        if s["supplier_pos"] != "footer":
            self.text(self.m, self.y, address, 7.5)
            self.y -= 10
            self.text(self.m, self.y, taxid, 7.5)
            self.y -= 14
        self.text(self.w / 2, self.y, title, 11 if receipt else 13, "c")
        self.y -= 18
        self.header_fields()
        if s["supplier_pos"] != "buyer_first":
            for row in buyer_block:
                self.text(self.m, self.y, row, 8.5)
                self.y -= 11
        if s["cur_mode"] == "sentence":
            self.text(self.m, self.y, f"All amounts in {CURRENCIES[s['totals_values']['code']][1]}", 8)
            self.y -= 11
        self.y -= 6

    def header_fields(self):
        s = self.s
        fields = self.head_fields()
        layout = "stacked" if s.get("receipt") else s["head"]
        sep = s["sep"]
        if layout == "boxes":
            n = len(fields)
            per = min(n, 4)
            cw = (self.w - 2 * self.m) / per
            for i, (label, value) in enumerate(fields):
                col, row = i % per, i // per
                x, y = self.m + col * cw, self.y - row * 30
                self.c.rect(x, y - 26, cw, 28, stroke=1, fill=0)
                self.text(x + 3, y - 8, label, 7)
                self.text(x + 3, y - 21, value, 9)
            self.y -= 30 * ((n - 1) // per + 1) + 6
        elif layout == "inline":
            rows, row = [], ""
            for label, value in fields:
                piece = f"{label}{sep}{value}"
                if self.width(row + "     " + piece) > self.w - 2 * self.m:
                    rows.append(row)
                    row = piece
                else:
                    row = f"{row}     {piece}" if row else piece
            for row in rows + [row]:
                self.text(self.m, self.y, row, 9)
                self.y -= 12
            self.y -= 4
        elif layout == "two_col":
            half = self.w / 2
            for i, (label, value) in enumerate(fields):
                x = self.m if i % 2 == 0 else half
                self.text(x, self.y, f"{label}{sep}{value}", 9)
                if i % 2:
                    self.y -= 12
            self.y -= 14 if len(fields) % 2 else 4
        elif layout == "right":
            for label, value in fields:
                self.text(self.w - self.m - 110, self.y, label, 9, "r")
                self.text(self.w - self.m, self.y, value, 9, "r")
                self.y -= 12
            self.y -= 4
        elif layout == "value_first":
            for label, value in fields:
                self.text(self.m, self.y, value, 9)
                self.text(self.m + 120, self.y, f": {label}", 9)
                self.y -= 12
            self.y -= 4
        elif s["lang"] == "ar":
            for label, value in fields:
                self.text(self.w - self.m, self.y, label, 9, "r")
                self.text(self.w - self.m - 130, self.y, value, 9, "r")
                self.y -= 12
            self.y -= 4
        else:
            for label, value in fields:
                self.text(self.m, self.y, f"{label}{sep}{value}", 9)
                self.y -= 12
            self.y -= 4

    # Table.
    def columns(self):
        s = self.s
        L = lambda k: self.label(k)  # noqa: E731
        has_sku = any(x["sku"] for x in self.lines)
        has_gtin = any(x["gtin"] for x in self.lines)
        amount_head = L("net_amount") + (f" ({s['totals_values']['code']})" if s["cur_mode"] == "column" else "")
        cols = []
        if has_sku:
            cols.append(("sku", L("sku"), 62, "l"))
        if has_gtin:
            cols.append(("gtin", L("gtin"), 74, "l"))
        cols += [("description", L("description"), 170, "l"), ("uom", L("uom"), 32, "l"), ("qty", L("qty"), 40, "r"), ("price", L("price"), 56, "r")]
        if s.get("line_discount"):
            cols.append(("discount", s["labels"]["discount"], 36, "r"))
        if s.get("multi_rate"):
            cols.append(("line_rate", s["labels"]["line_tax"], 34, "r"))
        cols.append(("net_amount", amount_head, 66, "r"))
        mode = s["columns"]
        if mode == "shuffled":
            self.rng.shuffle(cols)
        elif mode == "amount_first":
            cols = cols[-1:] + cols[:-1]
        elif mode == "merged_qty_price":
            keep = [c for c in cols if c[0] not in ("qty", "price")]
            keep.insert(-1, ("qty_price", f"{L('qty')} x {L('price')}", 96, "r"))
            cols = keep
        elif mode == "merged_code_desc" and has_sku:
            cols = [("code_desc", f"{L('sku')} / {L('description')}", 232, "l")] + [c for c in cols if c[0] not in ("sku", "description")]
        elif mode == "merged_qty_uom":
            keep = [c for c in cols if c[0] not in ("qty", "uom")]
            keep.insert(-2, ("qty_uom", L("qty"), 64, "r"))
            cols = keep
        if s.get("glued"):
            keep = [c for c in cols if c[0] not in ("qty", "uom")]
            keep.insert(-2, ("qty_uom_glued", L("qty"), 56, "r"))
            cols = keep
        total = sum(c[2] for c in cols)
        scale = (self.w - 2 * self.m) / total
        return [(k, h, wd * scale, a) for k, h, wd, a in cols]

    def cell(self, line, key):
        s = self.s
        tv = s["totals_values"]
        sign = tv["sign"] if not s.get("credit_positive") else Decimal(1)
        if key == "qty":
            return f"{line['qty'].normalize():f}" if s["numfmt"] != "arabic" else f"{line['qty'].normalize():f}".translate(AR_DIGITS)
        if key == "price":
            return self.num(line["price"])
        if key == "net_amount":
            return self.num(sign * line["net_amount"])
        if key == "discount":
            return f"{line['discount'].normalize():f}%"
        if key == "line_rate":
            return f"{line['line_rate'].normalize():f}%"
        if key == "qty_price":
            return f"{line['qty'].normalize():f} x {self.num(line['price'])}"
        if key == "code_desc":
            return f"{line['sku']} {line['description']}"
        if key == "qty_uom":
            return f"{line['qty'].normalize():f} {line['uom']}"
        if key == "qty_uom_glued":
            return f"{line['qty'].normalize():f}{line['uom']}"
        value = line.get(key)
        if value and key == "description" and any("؀" <= c <= "ۿ" for c in value):
            return shape_arabic(value)
        return value or ""

    def table_head(self, cols):
        s = self.s
        x = self.m
        size = 7.5 if len(cols) > 6 else 8
        if s["table"] in ("grid", "zebra", "lines"):
            self.c.line(self.m, self.y + 10, self.w - self.m, self.y + 10)
        for key, head, wd, align in cols:
            self.text(x + 2 if align == "l" else x + wd - 2, self.y, head[:int(wd / 3.7)], size, align)
            x += wd
        if s["table"] in ("grid", "zebra", "lines"):
            self.c.line(self.m, self.y - 3, self.w - self.m, self.y - 3)
        self.y -= 13

    def wrap(self, value, wd, size):
        words, rows, row = str(value).split(" "), [], ""
        for word in words:
            trial = f"{row} {word}".strip()
            if self.width(trial, size) > wd - 4 and row:
                rows.append(row)
                row = word
            else:
                row = trial
        return rows + [row]

    def table(self):
        s = self.s
        if s.get("receipt"):
            return self.receipt_table()
        cols = self.columns()
        self.table_head(cols)
        size = 7.5
        running = Decimal(0)
        sign = s["totals_values"]["sign"] if not s.get("credit_positive") else Decimal(1)
        for i, line in enumerate(self.lines):
            cells = {k: self.cell(line, k) for k, *_ in cols}
            rows = 1
            wrapped = {}
            for key, _, wd, _a in cols:
                if key in ("description", "code_desc"):
                    wrapped[key] = self.wrap(cells[key], wd, size)
                    rows = max(rows, len(wrapped[key]))
            need = 11 * rows + (10 if line.get("subrow") else 0) + 2
            if self.y - need < 70:
                if s.get("carry"):
                    self.text(self.w - self.m, self.y - 4, f"Carried Forward  {self.num(sign * running)}", 8, "r")
                self.new_page()
                self.table_head(cols)
                if s.get("carry"):
                    self.text(self.w - self.m, self.y, f"Brought Forward  {self.num(sign * running)}", 8, "r")
                    self.y -= 13
            top = self.y
            if s["table"] == "zebra" and i % 2:
                self.c.setFillGray(.92)
                self.c.rect(self.m, top - 11 * rows + 8, self.w - 2 * self.m, 11 * rows, stroke=0, fill=1)
                self.c.setFillGray(0)
            x = self.m
            for key, _, wd, align in cols:
                if key in wrapped:
                    for j, row in enumerate(wrapped[key]):
                        self.text(x + 2, top - 11 * j, row, size)
                elif s.get("glued") and key == "price":
                    self.text(x + wd, top, cells[key], size, "r")  # Touches the next cell: columns printed too tight.
                elif s.get("glued") and key == "net_amount":
                    self.text(x, top, cells[key], size, "l")
                else:
                    self.text(x + 2 if align == "l" else x + wd - 2, top, cells[key], size, align)
                x += wd
            self.y -= 11 * rows
            if line.get("subrow"):
                self.text(self.m + 8, self.y, line["subrow"], 6.5)
                self.y -= 10
            if s["table"] == "grid":
                self.c.setLineWidth(.3)
                self.c.line(self.m, self.y + 7, self.w - self.m, self.y + 7)
                self.c.setLineWidth(1)
            self.y -= 2
            running += line["net_amount"]
        if s["table"] in ("grid", "lines"):
            self.c.line(self.m, self.y + 8, self.w - self.m, self.y + 8)
        self.y -= 8

    def receipt_table(self):
        for line in self.lines:
            self.text(self.m, self.y, f"{line['sku'] or ''} {line['description']}"[:44], 7)
            self.y -= 9
            self.text(self.m + 6, self.y, f"{line['qty'].normalize():f} {line['uom']} x {self.num(line['price'])}", 7)
            self.text(self.w - self.m, self.y, self.num(line["net_amount"]), 7, "r")
            self.y -= 11

    # Totals.
    def total_rows(self):
        s, tv = self.s, self.s["totals_values"]
        rows = []
        if s.get("doc_discount"):
            rows.append((self.rng.choice(("Gross Amount", "Total Before Discount", "Items Total")), self.shown(tv["subtotal"])))
            rows.append((self.rng.choice(("Less: Discount", "Trade Discount", "Discount")), tv["doc_discount"]))
        if s["print_net"]:
            rows.append((self.label("net"), self.shown(tv["net"])))
        if s["print_tax"]:
            rows.append((self.label("tax"), self.shown(tv["tax"])))
        rows.append((self.label("gross"), self.shown(tv["gross"])))
        return rows

    def glue(self, label, value):
        return f"{label}{value}" if self.s.get("glued") else f"{label}{self.s['sep']}{value}"

    def totals(self):
        s = self.s
        rows = self.total_rows()
        layout = "right" if s.get("receipt") else s["totals"]
        need = 16 * len(rows) + 40
        if layout not in ("strip_above",) and self.y - need < 60:
            self.new_page()
        if s.get("zero"):
            self.text(self.m, self.y, self.rng.choice(("FREE OF CHARGE", "FOC - samples, no commercial value", "No charge")), 8)
            self.y -= 12
        if layout == "inline":
            self.text(self.m, self.y, "   ".join(self.glue(k, self.num(v, total=True)) for k, v in rows), 9)
            self.y -= 14
        elif layout == "vat_table":
            self.vat_table(rows)
        elif layout == "strip_below":
            self.strip(rows)
        elif layout == "strip_above":
            pass
        else:
            left = layout == "left"
            for label, value in rows:
                text = self.num(value, total=True)
                if s.get("glued"):
                    self.text(self.w - self.m, self.y, self.glue(label, text), 9, "r")
                elif s["lang"] == "ar":
                    self.text(self.m + 90, self.y, text, 9, "r")
                    self.text(self.m + 240, self.y, label, 9, "r")
                elif left:
                    self.text(self.m, self.y, label, 9)
                    self.text(self.m + 190, self.y, text, 9, "r")
                else:
                    self.text(self.w - self.m - 95, self.y, label, 9, "r")
                    self.text(self.w - self.m, self.y, text, 9, "r")
                self.y -= 13
        self.extras()

    def strip(self, rows):
        cw = (self.w - 2 * self.m) / len(rows)
        for i, (label, value) in enumerate(rows):
            x = self.m + i * cw
            self.c.rect(x, self.y - 24, cw, 32, stroke=1, fill=0)
            self.text(x + 3, self.y - 3, label[:int(cw / 4)], 7)
            self.text(x + 3, self.y - 18, self.num(value, total=True), 9)
        self.y -= 36

    def vat_table(self, rows):
        s, tv = self.s, self.s["totals_values"]
        heads = ("Rate", "Taxable", "VAT", "Total")
        cw = 72
        x0 = self.w - self.m - cw * 4
        self.text(x0, self.y, "VAT Summary", 8)
        self.y -= 11
        for i, head in enumerate(heads):
            self.text(x0 + cw * (i + 1) - 3, self.y, head, 8, "r")
        self.y -= 11
        values = (f"{s['vat_rate'].normalize():f}%", self.num(self.shown(tv["net"])), self.num(self.shown(tv["tax"])), self.num(self.shown(tv["gross"])))
        for i, value in enumerate(values):
            self.text(x0 + cw * (i + 1) - 3, self.y, value, 8.5, "r")
        self.y -= 16
        for label, value in rows[-1:]:
            self.text(self.w - self.m - 95, self.y, label, 9, "r")
            self.text(self.w - self.m, self.y, self.num(value, total=True), 9, "r")
            self.y -= 13

    def extras(self):
        s, tv, rng = self.s, self.s["totals_values"], self.rng
        count = len(self.lines)
        qty_total = sum(x["qty"] for x in self.lines)
        if s.get("near"):
            wrong = abs(tv["net"]) + Decimal(rng.randrange(100, 900))
            self.text(self.m, self.y, f"Last invoice {self.s['labels']['net']}: {self.num(wrong)}   Rate {s['vat_rate'].normalize():f}%", 8)
            self.y -= 11
            self.text(self.m, self.y, f"Discount offered next order: {self.num(wrong / 10)}", 8)
            self.y -= 11
        if s.get("misleading"):
            block = [f"Total lines: {count}", f"Total Qty: {qty_total.normalize():f}", f"Total Weight (kg): {rng.randrange(100, 9000) / 10:.2f}",
                     f"Net weight: {rng.randrange(100, 9000) / 10:.2f} kg", f"Gross weight: {rng.randrange(100, 9000) / 10:.2f} kg", rng.choice(_TERMS),
                     f"Previous balance: {self.num(Decimal(rng.randrange(1000, 90000)))}", f"Amount paid: {self.num(Decimal(rng.randrange(100, 900)))}",
                     f"Tax Reg. No: {s['trn']}", f"VAT rate {s['vat_rate'].normalize():f}%", f"Tax Point {s['printed_date']}",
                     f"Total pages {self.page}", f"Cartons {rng.randrange(2, 40)}", f"Bank: {rng.choice(('USD', 'GBP', 'SAR'))} account 00{rng.randrange(10**8, 10**9)}"]
            rng.shuffle(block)
            for a, b in zip(block[:6:2], block[1:6:2]):
                self.text(self.m, self.y, a, 8)
                self.text(self.w / 2, self.y, b, 8)
                self.y -= 11
        if s.get("dual"):
            code, rate = s["dual_code"], s["dual_rate"]
            self.text(self.m, self.y, f"Exchange rate {tv['code']}/{code} {rate}", 8)
            self.y -= 11
            self.text(self.m, self.y, f"Total equivalent in {code} {self.num(abs(tv['gross']) * rate)}", 8)
            self.y -= 11
        if s.get("credit_positive") or s.get("credit"):
            self.text(self.m, self.y, "Credit against returned goods.", 8)
            self.y -= 11

    def render(self):
        self.header()
        s = self.s
        if not s.get("receipt") and s["totals"] == "strip_above":
            self.strip(self.total_rows())
        self.table()
        self.totals()
        self.footer()
        self.c.save()


def render_pdf(inv: Invoice, path: Path) -> Path:
    _Doc(inv, Path(path)).render()
    return Path(path)
