"""Header fields read from the labels a page prints, on layouts and wordings no template knows.

A row is cut into tokens and the tokens into labels. Every label that names something, wanted here or not, ends the
value before it: "Invoice No.: SI/1 Date of Issue: 2026-01-02" holds a number and a date, "Order No: 77" holds neither.
A label opens at the start of a row, after a gap, or after a value, so the same words inside a sentence are no label.

A value sits in one of three places: after its label; before it ("K505 : Tax Invoice No"); or under it in a strip of
headings. It must have the type of its field, and two different values at the same strength leave the field blank.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import product
from typing import Any

from app.layout_extract import (
    _SAFE_IDENTIFIER,
    _Word,
    _clean_party_name,
    _heading_of_another_kind,
    _money_on_later_page,
    _only_distinct,
    _parse_date,
    _row_numbers,
    _row_y,
    _summary_columns,
)
from app.numbers import _ARABIC, ISO_CURRENCIES


def _phrases(*specs: str) -> set[str]:
    """ "a|b c|" spells "ac", "a", "bc", "b": each space-separated slot holds alternatives, an empty one optional."""

    return {"".join(parts) for spec in specs for parts in product(*(slot.split("|") for slot in spec.split(" ")))}


_NUMBER_WORD = "no|nr|nbr|num|number|#"
_EXCLUDED = "excl|exc|exl|excluding|exclusive|ex|pre|before|without"
_TAX_WORD = "vat|tax|gst"

# Strongest first: a weaker wording is read only when no stronger one printed a value.
_LABELS: dict[str, tuple[set[str], ...]] = {
    "number": (
        _phrases(f"|tax|vat|gst|sales|commercial invoice|inv {_NUMBER_WORD}|ref|reference|code|id"),
        _phrases(f"bill|document|doc|voucher|receipt|memo|return {_NUMBER_WORD}|ref|reference|id", "folio",
                 f"folio {_NUMBER_WORD}", f"credit|debit note|memo {_NUMBER_WORD}|ref|reference", f"cn {_NUMBER_WORD}"),
        _phrases("our ref|reference", f"ref|reference {_NUMBER_WORD}", f"serial {_NUMBER_WORD}"),
    ),
    "date": (
        _phrases("invoice|inv|bill|billing|document|doc|issue date|dt", "date of issue|invoice|bill|document",
                 "date issued", "issued on"),
        {"date", "dated"},
        _phrases("date of supply", "supply date", "tax point |date"),
    ),
    "po": (
        _phrases(f"purchase order |{_NUMBER_WORD}|ref|reference", f"po|lpo |{_NUMBER_WORD}|ref|reference",
                 f"customer|client|buyer|buyers|your po|lpo |{_NUMBER_WORD}|ref|reference",
                 f"customer|client|buyer|buyers|your order |{_NUMBER_WORD}|ref|reference", "order ref|reference"),
        _phrases("your ref|reference"),
    ),
    "currency": (
        _phrases("currency|curr|cur|ccy |code", "invoice|document|billing currency"),
    ),
    "net": (
        _phrases("net total|amount|value", "total net", "total net amount", "taxable |amount|value|supplies|total",
                 "total taxable |amount|value|supplies", "untaxed amount", "total untaxed |amount",
                 f"total|amount|value|invoice|invoicetotal|totalamount {_EXCLUDED} of| {_TAX_WORD} |total|amount",
                 f"{_EXCLUDED} of| {_TAX_WORD} |total|amount", "goods value|total", "merchandise total"),
        {"subtotal"},
        {"net"},
    ),
    "tax": (
        _phrases(f"total {_TAX_WORD}|iva|tva |amount|amt", f"{_TAX_WORD}|iva|tva total|amount|amt",
                 f"output {_TAX_WORD} |amount", "sales tax |amount|total", f"add {_TAX_WORD}"),
        _phrases("vat|tax|gst|iva|tva|mwst"),
    ),
    "supplier": (
        _phrases("supplied|billed|sold by", "billed from", "supplier |name", "seller |name", "vendor |name"),
        # Open-ended: a person, an office or a route prints these too.
        _phrases("issued by", "from"),
    ),
}

# Labels of what is not wanted. They are read so that they end the value before them and take the value after them.
_OTHER = _phrases(
    # Other documents' numbers
    f"delivery|dispatch|despatch note |{_NUMBER_WORD}", f"dn|do|grn|gdn {_NUMBER_WORD}",
    f"order|salesorder|so|quotation|quote|contract|job|account|acct|customer|client|supplier|vendor|delivery "
    f"{_NUMBER_WORD}|code|id|ref|reference",
    f"against|reference|ref|original|previous|last|related|proforma|cancelled invoice "
    f"|{_NUMBER_WORD}|ref|reference",
    f"vat|tax|gst|trn|tin|cr|licence|license reg|registration |{_NUMBER_WORD}", f"vat|tax|gst|trn|tin {_NUMBER_WORD}|id",
    "trn", "gstin", "pobox", "tax invoice", "invoice", "tax date",
    # Other dates
    "due|delivery|order|po|lpo|ship|shipping|shipment|dispatch|despatch|expiry|payment|print|printed|run|report|"
    "posting|value|valid|validity date", "date of delivery|order|shipment|dispatch|despatch|print", "due on",
    "valid until", "pay by", "payment due",
    # Other figures
    "vat|tax|gst rate|%", "total|grand|invoice|gross total|amount|value|payable|due", "invoice total|amount|value",
    "amount|balance|net|netamount|totalamount|invoiceamount due|payable", "total incl vat|tax|gst",
    "total including|inclusive of| vat|tax|gst", "amount incl|including|inclusive of| vat|tax|gst", "total|gross", "total",
    "incl|including|inclusive of| vat|tax|gst", "balance", "discount", "freight", "shipping", "rounding", "net weight|wt",
    "gross weight|wt", "subtotal carried|brought forward", "carried|brought forward", "rate", "qty",
    "quantity", "unit price", "price", "description", "uom",
    # Other parties and details
    "bill|invoice|sold|ship|deliver|delivered to", "buyer |name", "customer |name", "client |name", "consignee",
    "address", "attn", "attention", "tel", "telephone", "phone", "mobile", "fax", "email", "web", "website",
    "page", "payment terms|method|mode", "terms", "salesman", "sales person", "salesperson", "prepared|approved by",
    "bank", "iban", "swift",
)

_BY_PHRASE: dict[str, tuple[str, int]] = {phrase: ("other", 0) for phrase in _OTHER}
for _field, _tiers in _LABELS.items():
    for _tier, _spelled in enumerate(_tiers):
        for _phrase in _spelled:
            _BY_PHRASE[_phrase] = (_field, _tier)
_BY_PHRASE.pop("", None)
_CURRENCY_SIGN = {"€": "EUR", "£": "GBP", "BD": "BHD", "QR": "QAR", "KD": "KWD", "SR": "SAR", "RO": "OMR", "Dh": "AED", "Dhs": "AED"}
_CONVERSION = re.compile(r"(?i)\b(?:equivalent|exchange\s+rate|rate\s+of\s+exchange|converted)\b")
# Where a text layer or a scan runs a label into its figure: "Supplies€812.00", "5%97.50", "Value809102.127", "TaxAED".
_GLUE = re.compile(r"(?<=%)(?=\S)|(?<=[A-Za-z])(?=[€£$][0-9])|(?<=[a-z])(?=[0-9][0-9,.'’]*[.,][0-9]+$)|(?<=[a-z])(?=[A-Z]{2,3}[-/]?[0-9 ]*$)")
# A word that makes the label after it name something else: "Non Taxable Amount", "VAT on Taxable", "Bank Currency".
_QUALIFIERS = {"non", "exempt", "on", "account", "acct", "base", "bank", "ac", "foreign", "reporting", "beneficiary",
               "remit", "remittance", "settlement"}
_GROUP_START = re.compile(r"^[0-9]{3}(?:[.,][0-9]+)?")
_LONGEST = 7
_TAXABLE_LEADS = {"total", "subtotal", "net", "invoice"}
_CELL_GAP = 1.2

_SEPARATOR = {":", "#", "-", "=", "|", ";", "：", "."}
_RATE = re.compile(r"^[@(]*[0-9]+(?:\.[0-9]+)?\s*%?\)?$")
_DATE_SHAPE = re.compile(r"^(?:[0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4})$")
_AMOUNT_SHAPE = re.compile(r"^[0-9]{1,3}(?:[,'’ ][0-9]{3})*[.,][0-9]{2,3}$")
_ISO_IN_BRACKETS = re.compile(r"^\(([A-Z]{3})\)$")
_BANK_LABEL = re.compile(r"(?i)^(?:iban|swift|bank|account|acct)\b")
_LINE_HEADING = re.compile(r"(?i)\b(?:description|qty|quantity|price|uom)\b")
_MONEY_LABEL = re.compile(r"(?i)\b(?:total|amount|balance|payable|due)\b")
_LETTERS = re.compile(r"^[^\W\d_]+\.?$")
_BARE_DIRHAM = re.compile(r"(?i)\bdirhams?\b")
_UAE = re.compile(r"(?i)\b(?:uae|u\.a\.e\.?|emirati|emirates|aed)\b")
_CURRENCY_NAMES = (
    (r"\bsaudi\s+riyals?\b", "SAR"),
    (r"\bqatari\s+riyals?\b", "QAR"), (r"\bomani\s+(?:riyals?|rials?)\b|\brials?\s+omani\b", "OMR"),
    (r"\bbahraini\s+dinars?\b", "BHD"), (r"\bkuwaiti\s+dinars?\b", "KWD"), (r"\bjordanian\s+dinars?\b", "JOD"),
    (r"\begyptian\s+pounds?\b", "EGP"), (r"\bindian\s+rupees?\b", "INR"), (r"\bus\s+dollars?\b", "USD"),
    (r"\beuros?\b", "EUR"), (r"\bpounds?\s+sterling\b|\bbritish\s+pounds?\b", "GBP"), (r"\bswiss\s+francs?\b", "CHF"),
    (r"\bjapanese\s+yen\b", "JPY"),
)
_AMOUNTS_IN = re.compile(r"(?i)\b(?:all\s+)?(?:amounts?|figures|prices|values)\s+(?:are\s+|shown\s+|stated\s+)*in\s+(.+)$")


def _arabic_key(words: list[str]) -> str:
    """A phrase however the reader ordered it: a text layer holds Arabic in visual order, an OCR model in logical."""

    plain = [re.sub("[أإآ]", "ا", unicodedata.normalize("NFKC", word)) for word in words]
    return " ".join(sorted(min(word, word[::-1]) for word in plain))


_ARABIC_LABELS = {
    _arabic_key(phrase.split()): label
    for phrase, label in {
        "رقم الفاتورة": "invoice number", "فاتورة رقم": "invoice number", "تاريخ الفاتورة": "invoice date",
        "التاريخ": "date", "تاريخ": "date", "تاريخ الاستحقاق": "due date", "أمر الشراء": "purchase order",
        "رقم أمر الشراء": "purchase order", "أمر شراء": "purchase order", "العملة": "currency", "المورد": "supplier",
        "البائع": "supplier", "العميل": "customer", "المشتري": "buyer", "المجموع الفرعي": "subtotal",
        "المبلغ الخاضع للضريبة": "taxable amount", "الصافي": "net amount", "ضريبة القيمة المضافة": "vat",
        "الضريبة": "vat", "قيمة الضريبة": "vat amount", "الإجمالي": "total", "المجموع": "total",
        "الإجمالي شامل الضريبة": "total", "الاجمالي شامل الضريبة": "total", "فاتورة ضريبية": "tax invoice",
        "المجموع قبل الضريبة": "taxable amount", "الإجمالي قبل الضريبة": "taxable amount",
        "المبلغ قبل الضريبة": "taxable amount", "المبلغ غير شامل الضريبة": "taxable amount",
        "الإجمالي غير شامل الضريبة": "taxable amount",
    }.items()
}
_ARABIC_LETTERS = re.compile(r"[\u0600-\u06FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_LATIN_LETTERS = re.compile(r"[A-Za-z]")


def _read_arabic(row: list[_Word]) -> list[_Word]:
    """A row with Arabic words, in the form the labels below read.

    Beside English labels the Arabic only repeats them, so it is dropped. Alone it names the field in its own words,
    printed to the right of the value it labels, which is the "value : label" form.
    """

    row = [replace(word, text=word.text.translate(_ARABIC)) for word in row]
    if not any(_ARABIC_LETTERS.search(word.text) for word in row):
        return row
    latin = [word for word in row if not _ARABIC_LETTERS.search(word.text)]
    if row_labels(latin) and any(_LATIN_LETTERS.search(word.text) for word in latin):
        return latin
    out: list[_Word] = []
    index = 0
    while index < len(row):
        # The whole run of touching Arabic words is the label: "before tax" is no tax however it ends.
        hit = index
        while hit < len(row) and _ARABIC_LETTERS.search(row[hit].text) and (
            hit == index or row[hit].x0 - row[hit - 1].x1 < row[hit].height * 0.6
        ):
            hit += 1
        run = row[index:hit]
        if not run or _arabic_key([word.text for word in run]) not in _ARABIC_LABELS:
            out.extend(run or row[index:index + 1])
            index = max(hit, index + 1)
            continue
        if out and out[-1].text not in {":", "："}:
            out.append(replace(run[0], text=":", x0=run[0].x0, x1=run[0].x0))
        out.append(replace(run[0], text=_ARABIC_LABELS[_arabic_key([w.text for w in run])], x1=run[-1].x1))
        index = hit
    return out


@dataclass(frozen=True)
class _Label:
    field: str
    tier: int
    start: int
    end: int
    left: float
    right: float


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9#%]+", "", text.casefold())


def tokens(row: list[_Word]) -> list[_Word]:
    """The row's words, phrase boxes split on their spaces, and a colon cut from the word it is printed on."""

    out: list[_Word] = []
    for word in sorted(row, key=lambda item: item.x):
        text = word.text
        per_char = (word.x1 - word.x0) / max(1, len(text))
        for match in re.finditer(r"[^\s:：]+|[:：]", text):
            start = match.start()
            for cut in [*(found.start() for found in _GLUE.finditer(match[0]) if found.start()), len(match[0])]:
                end = match.start() + cut
                out.append(replace(word, text=text[start:end], x0=word.x0 + start * per_char, x1=word.x0 + end * per_char))
                start = end
    return _read_arabic(_joined_figures(_joined_initials(out)))


def _joined_initials(row: list[_Word]) -> list[_Word]:
    """Initials an OCR model cut at their stops ("P . O .") are one word again."""

    out: list[_Word] = []
    for word in row:
        if word.text == "." and out and re.fullmatch(r"(?:[A-Za-z]\.)*[A-Za-z]", out[-1].text):
            out[-1] = replace(out[-1], text=out[-1].text + ".", x1=word.x1)
        elif len(word.text) == 1 and word.text.isalpha() and out and re.fullmatch(r"(?:[A-Za-z]\.)+", out[-1].text):
            out[-1] = replace(out[-1], text=out[-1].text + word.text, x1=word.x1)
        else:
            out.append(word)
    return out


def _joined_figures(row: list[_Word]) -> list[_Word]:
    """A figure an OCR model cut at its commas ("KD1 , 277 , 136.393") is one figure again."""

    out: list[_Word] = []
    for word in row:
        if len(out) >= 2 and out[-1].text == "," and out[-2].text[-1:].isdigit() and _GROUP_START.match(word.text):
            out[-2:] = [replace(out[-2], text=f"{out[-2].text},{word.text}", x1=word.x1)]
        else:
            out.append(word)
    return out


def _opens(row: list[_Word], index: int) -> bool:
    """A label opens the row, follows a gap, or follows a value or colon: inside a sentence the words are no label."""

    if index == 0:
        return True
    before, here = row[index - 1], row[index]
    if _compact(before.text) in _QUALIFIERS:
        return False
    return (
        here.x0 - before.x1 >= here.height * 0.6
        or before.text.endswith((":", "：", "|", ")"))
        or any(character.isdigit() for character in before.text)
        or not any(character.isalpha() for character in before.text)
    )


def _rate_end(row: list[_Word], index: int) -> int:
    """Past a rate printed with a tax label: "VAT @ 5 %", "VAT (5%)", "VAT 5%", "VAT @ 5.00"; a bare figure is not one."""

    at = False
    while index < len(row) and row[index].text in {"@", "("}:
        at = at or row[index].text == "@"
        index += 1
    if index < len(row) and _RATE.match(row[index].text):
        at = at or row[index].text.startswith("@")
        end = index + 2 if index + 1 < len(row) and row[index + 1].text.strip(")") == "%" else index + 1
        if at or "%" in row[end - 1].text:
            return end + 1 if end < len(row) and row[end].text == ")" else end
    return index


def row_labels(row: list[_Word]) -> list[_Label]:
    """Every label in the row, longest wording first, left to right."""

    found: list[_Label] = []
    figures = any(_row_numbers([word]) and "%" not in word.text for word in row)
    index = 0
    while index < len(row):
        hit = None
        if _opens(row, index) and _compact(row[index].text):
            # In a strip of headings a wording's words touch: "VAT" and "Total" in two columns are two headings.
            reach = index + 1
            while reach < min(len(row), index + _LONGEST) and (
                figures or row[reach].x0 - row[reach - 1].x1 < row[reach].height * _CELL_GAP
            ):
                reach += 1
            for end in range(reach, index, -1):
                entry = _BY_PHRASE.get(_compact(" ".join(word.text for word in row[index:end])))
                if entry is not None and _compact(row[end - 1].text):
                    hit = (entry, end)
                    break
        if hit is None:
            index += 1
            continue
        (field, tier), end = hit
        # "PO No : LPO 923": the value printed behind a colon carries the label's own word.
        if found and found[-1].field == field and found[-1].end == index - 1 and row[index - 1].text in _SEPARATOR:
            index = end
            continue
        # "Not Taxable Amount 50.00": a word in front of "Taxable" other than a few leads makes it some other figure.
        if (
            figures and index and field == "net" and _compact(row[index].text) == "taxable"
            and _LATIN_LETTERS.search(row[index - 1].text)
            and _compact(row[index - 1].text) not in _TAXABLE_LEADS
        ):
            index = end
            continue
        if field == "tax":
            end = _rate_end(row, end)
        found.append(_Label(field, tier, index, end, row[index].x0, row[end - 1].x1))
        index = end
    return found


def starts_label(row: list[_Word], index: int) -> bool:
    """Whether a label opens at this word: an identifier being read off the row stops here."""

    return any(label.start == index for label in row_labels(row))


def _identifier(words: list[_Word]) -> str | None:
    """An identifier at the head of the words: its parts touch, it holds a digit, and it is no date and no amount."""

    pieces: list[str] = []
    previous = None
    joined = False
    trailing = False
    for position, word in enumerate(words):
        text = word.text.strip(",;")
        # An OCR model cuts an identifier at its marks: "LPO / 923".
        if text in {"/", "-", "."} and pieces:
            pieces[-1] += text
            joined = True
            previous = word
            continue
        if joined:
            pieces[-1] += text
            joined = False
            previous = word
            continue
        if not re.fullmatch(r"[A-Za-z0-9./_-]+", text):
            break
        if previous is not None and word.x0 - previous.x1 > word.height * 1.5:
            break
        has_digit = any(part.isdigit() for piece in pieces for part in piece)
        if has_digit and _DATE_SHAPE.match(text):
            break
        if has_digit and _LETTERS.match(text):
            # A word before a colon is a label, known or not. Any other is part of the value ("7781 B"), or prose.
            following = words[position + 1:position + 2]
            if following and following[0].text in {":", "："}:
                break
            if following and _LETTERS.match(following[0].text) and following[0].x0 - word.x1 <= word.height * 1.5:
                return None
            pieces.append(text)
            trailing = True
            break
        pieces.append(text)
        previous = word
        # "PO 7920, Dubai": a comma ends the value.
        if word.text.endswith(",") and any(character.isdigit() for character in text):
            break
    if not any(character.isdigit() for piece in pieces for character in piece):
        return None
    if len(pieces) > 3 or sum(not any(c.isdigit() for c in piece) for piece in pieces) > 1 + trailing:
        return None
    value = " ".join(pieces)
    plain = value.replace(" ", "")
    # "P.O. Box7920", "P.O. Bag 7920" are addresses.
    if re.match(r"(?i)(?:box|bag|bx)[0-9]", plain):
        return None
    if len(plain) > 80 or not _SAFE_IDENTIFIER.fullmatch(plain):
        return None
    if is_date_like(value) or _AMOUNT_SHAPE.match(value):
        return None
    return value.rstrip(".")


def is_date_like(value: str) -> bool:
    return bool(_DATE_SHAPE.match(value.strip())) or _parse_date(value) is not None


def _date(words: list[_Word]) -> str | None:
    for length in (5, 4, 3, 1):
        parsed = _parse_date(" ".join(word.text for word in words[:length]).replace(",", " "))
        if parsed is not None:
            return parsed
    return None


def _currency(words: list[_Word]) -> str | None:
    if not words:
        return None
    code = words[0].text.strip(".,;()")
    if code in ISO_CURRENCIES:
        return code
    return _currency_name(" ".join(word.text for word in words[:4]))


def _currency_name(text: str) -> str | None:
    """The one currency a sentence names. Many countries print dirhams: alone the word says none, so it stands for
    an unknown one against any other name. Two names say neither."""

    names = {code for pattern, code in _CURRENCY_NAMES if re.search(rf"(?i){pattern}", text)}
    names |= {word for word in re.findall(r"\b[A-Z]{3}\b", text) if word in ISO_CURRENCIES}
    if _BARE_DIRHAM.search(text):
        names.add("AED" if _UAE.search(text) else "dirham")
    return names.pop() if len(names) == 1 and "dirham" not in names else None


def _figure(words: list[_Word], field: str, tier: int, rated: bool) -> Decimal | None:
    """The one figure among the words, or none. A whole small number after a tax word is a rate, a long one a code."""

    kept = []
    for word in words:
        text = word.text.strip()
        if text.upper().strip("().") in ISO_CURRENCIES and not any(_row_numbers([other]) for other in kept):
            continue
        if text.upper().strip("().") in ISO_CURRENCIES:
            break
        if not _row_numbers([word]):
            break
        kept.append(word)
    numbers = _row_numbers(kept)
    if len(numbers) != 1:
        return None
    # Only a code may follow the figure. "1,200.00 CR" is a credit whose sign is left to review, "12 Items" a count,
    # "12 / 03 / 2099" a date.
    after = words[words.index(kept[-1]) + 1:]
    if after and after[0].text.upper().strip(".") not in ISO_CURRENCIES:
        return None
    if _DATE_SHAPE.match(kept[-1].text):
        return None
    value = numbers[0]
    whole = not re.search(r"[.,'’ ]", " ".join(word.text.strip() for word in kept))
    # "Net Amount BD12345" is a reference: two letters mark money only on a figure with decimals.
    if whole and re.match(r"^[A-Za-z]{2}[0-9]", kept[0].text.strip()):
        return None
    if field == "tax" and whole and not rated:
        return None
    if field == "net" and whole and _LABELS["net"][tier] == {"net"}:
        return None
    return value


def _value(field: str, tier: int, words: list[_Word], rated: bool = False) -> Any:
    if field in {"number", "po"}:
        return _identifier(words)
    if field == "date":
        return _date(words)
    if field == "currency":
        return _currency(words)
    if field in {"net", "tax"}:
        return _figure(words, field, tier, rated)
    if field == "supplier":
        return _clean_party_name(" ".join(word.text for word in words))
    return None


def _strip_separators(words: list[_Word]) -> list[_Word]:
    index = 0
    while index < len(words) and words[index].text in _SEPARATOR:
        index += 1
    return words[index:]


def _value_first(row: list[_Word], labels: list[_Label]) -> bool:
    """ "K505 : Tax Invoice No": the row opens on a value, and a colon stands before every label."""

    return bool(labels) and labels[0].start >= 2 and all(row[label.start - 1].text in {":", "："} for label in labels)


def _beside(row: list[_Word], labels: list[_Label]) -> list[tuple[str, int, Any]]:
    """Values printed on the label's own row, before or after it."""

    found: list[tuple[str, int, Any]] = []
    if _value_first(row, labels):
        start = 0
        for label in labels:
            if label.field != "other":
                value = _value(label.field, label.tier, row[start:label.start - 1])
                if value is not None:
                    found.append((label.field, label.tier, value))
            start = label.end
        return found
    for position, label in enumerate(labels):
        if label.field == "other":
            continue
        stop = labels[position + 1].start if position + 1 < len(labels) else len(row)
        words = _strip_separators(row[label.end:stop])
        if not words:
            continue
        # A total's figure sits at the page's far edge from its label; other values sit close to theirs.
        reach = words[0].page_width * 0.4 if label.field in {"net", "tax"} else max(36.0, words[0].height * 5, words[0].page_width * 0.2)
        if words[0].x0 - row[label.end - 1].x1 > reach:
            continue
        # "IBAN AE01 Currency USD" is the bank account's.
        if label.field == "currency" and any(_BANK_LABEL.search(word.text) for word in row[:label.start]):
            continue
        rated = any("%" in word.text for word in row[label.start:label.end])
        value = _value(label.field, label.tier, words, rated)
        if value is not None:
            # "PO 7920" with no mark or number word is as likely an address.
            bare = label.field == "po" and label.end - label.start == 1 and row[label.end].text not in _SEPARATOR
            found.append((label.field, max(label.tier, 1) if bare and _compact(row[label.start].text) in {"po", "lpo"} else label.tier, value))
    return found


def _cells(row: list[_Word]) -> list[list[_Word]]:
    cells: list[list[_Word]] = []
    for word in row:
        if cells and word.x0 - cells[-1][-1].x1 < word.height * _CELL_GAP:
            cells[-1].append(word)
        else:
            cells.append([word])
    return cells


def _overlap(left: float, right: float, label: _Label) -> float:
    return min(right, label.right) - max(left, label.left)


def _under(row: list[_Word], labels: list[_Label], below: list[_Word], after: list[list[_Word]]) -> list[tuple[str, int, Any]]:
    """Values in the row under a strip of headings, each in the one cell that sits under its own heading.

    A heading's words have no value beside them. Figures are read from a strip of nothing but money headings, with a
    single row of figures that adds up: anything else is a table of lines. The rows after it, up to the next label,
    must hold no other figure.
    """

    wanted = [label for label in labels if label.field != "other" and _strip_separators(
        row[label.end:next((other.start for other in labels if other.start >= label.end), len(row))]) == []]
    if not wanted or row_labels(below):
        return []
    known = {index for label in labels for index in range(label.start, label.end)}
    # The heading of a table of lines names the goods; a one-line table's figures are no totals.
    lines = any(_LINE_HEADING.search(" ".join(word.text for word in row[label.start:label.end])) for label in labels)
    more = False
    for later in after:
        named = row_labels(later)
        if len(named) > 1 or (named and named[0].start == 0):
            break
        # Another row of figures, or another value under one of these headings: a column, not a value.
        more = more or any(_row_numbers([word]) or (
            any(c.isdigit() for c in word.text) and any(_overlap(word.x0, word.x1, label) > 0 for label in wanted)
        ) for word in later)
    if lines or more:
        return []
    unknown = [word for index, word in enumerate(row) if index not in known and word.text not in _SEPARATOR]
    found: list[tuple[str, int, Any]] = []
    money: dict[str, Decimal] = {}
    columns: dict[str, float] = {}
    cells = _cells(below)
    for cell in cells:
        left, right = cell[0].x0, cell[-1].x1
        best = max(labels, key=lambda label: _overlap(left, right, label))
        if _overlap(left, right, best) <= 0:
            continue
        if best.field == "other":
            if _MONEY_LABEL.search(" ".join(word.text for word in row[best.start:best.end])):
                money["total"] = _figure(cell, "net", 0, True)
                columns["total"] = left
            continue
        if best not in wanted:
            continue
        # Under two headings at once, the values of two columns have run together.
        if best.field not in {"net", "tax"} and sum(_overlap(left, right, label) > 0 for label in labels) > 1:
            continue
        value = _value(best.field, best.tier, cell, rated=True)
        if best.field in {"net", "tax"}:
            if value is not None:
                money[best.field] = value
                columns[best.field] = left
        elif value is not None:
            found.append((best.field, best.tier, value))
    # Only the row's own sum proves figures are the totals: net, tax and their total, printed in that order.
    if not (unknown or len(cells) > len(labels)) and None not in (
        money.get("net"), money.get("tax"), money.get("total")
    ) and money["net"] + money["tax"] == money["total"] and columns["net"] < columns["tax"] < columns["total"]:
        found.extend((label.field, label.tier, money[label.field]) for label in wanted if label.field in money)
    return found


def _coded(words: list[_Word]) -> list[str]:
    """Each figure's own currency: a sign or code on it ("€812.00", "BD2.500", "12.00 EGP") or standing beside it."""

    found = []
    words = _strip_separators(words)
    for index, word in enumerate(words):
        if not _row_numbers([word]) and not re.search(r"[0-9]", word.text):
            continue
        glued = re.match(r"^([€£]|[A-Z]{2,3}|Dhs?)\.?(?=[0-9])", word.text) or re.search(r"(?<=[0-9])([A-Z]{3})$", word.text)
        if glued:
            found.append(_CURRENCY_SIGN.get(glued[1], glued[1]))
        # A code before the figure is its own only as the first thing after the label: "TOTAL FOR ALL 50.00" is prose.
        own = words[:index] in ([], words[:1]) or [word.text.casefold() for word in words[:index - 1]] == ["in"]
        for other in (words[index - 1] if index and own else None, words[index + 1] if index + 1 < len(words) else None):
            if other is not None:
                found.append(_CURRENCY_SIGN.get(other.text, other.text))
    return [code for code in found if code in ISO_CURRENCIES]


def _printed_currencies(row: list[_Word], labels: list[_Label]) -> list[str]:
    """What the row says the currency is: a bracketed code, a sentence naming it, a code or sign on a total's figure.

    A conversion line ("Total equivalent in EUR 12.50") prints another currency on purpose.
    """

    if _CONVERSION.search(" ".join(word.text for word in row)):
        return []
    found = [code[1] for word in row if (code := _ISO_IN_BRACKETS.match(word.text)) and code[1] in ISO_CURRENCIES]
    for position, label in enumerate(labels):
        if label.field in {"net", "tax"} or (label.field == "other" and _MONEY_LABEL.search(
                " ".join(word.text for word in row[label.start:label.end]))):
            stop = labels[position + 1].start if position + 1 < len(labels) else len(row)
            found.extend(_coded(row[label.end:stop]))
            if _value_first(row, labels):
                found.extend(_coded(row[labels[position - 1].end if position else 0:label.start]))
    sentence = _AMOUNTS_IN.search(" ".join(word.text for word in row))
    if sentence:
        named = _currency_name(sentence[1])
        if named:
            found.append(named)
    return found


def read_labels(rows: list[tuple[int, list[_Word]]]) -> dict[str, Any]:
    """Each field's value from its strongest wording, or absent when the page prints two different ones.

    "weak" names the fields whose value no wording proves: below the top tier, from a strip of headings, or open-ended.
    Money beside a label is left out of it: guard_figures holds that to the page's arithmetic.
    """

    tiered: dict[str, dict[int, list[Any]]] = {}
    printed: list[str] = []
    split = [(page, tokens(row)) for page, row in rows]
    strips: list[tuple[str, int, Any]] = []
    proven: set[tuple[str, Any]] = set()
    open_ended = False
    for index, (page, row) in enumerate(split):
        labels = row_labels(row)
        found = _beside(row, labels)
        proven |= {(field, value) for field, tier, value in found if tier == 0 or field in {"net", "tax"}}
        printed.extend(_printed_currencies(row, labels))
        if labels and not found and index + 1 < len(split) and split[index + 1][0] == page:
            below = split[index + 1][1]
            height = max(word.height for word in row)
            if below and 0 < _row_y(below) - _row_y(row) <= height * 3.5:
                found = _under(row, labels, below, [later for other, later in split[index + 2:] if other == page])
                # The summary table's own refusals hold for its figures here too: a heading printed twice or money on a
                # later page leaves no strip of figures read, on any page.
                strip = [item for item in found if item[0] in ("net", "tax")]
                found = [item for item in found if item[0] not in ("net", "tax")]
                if _heading_of_another_kind(row, split[index - 1][1] if index else []):
                    strip = []
                elif strip and any(
                    (other > page and _money_on_later_page(later)) or (later is not row and _summary_columns(later))
                    for other, later in split if later
                ):
                    strip, open_ended = [], True
                strips += strip
        for field, tier, value in found:
            tiered.setdefault(field, {}).setdefault(tier, []).append(value)
    for field, tier, value in () if open_ended else strips:
        tiered.setdefault(field, {}).setdefault(tier, []).append(value)
    result: dict[str, Any] = {}
    weak: set[str] = set()
    for field, tiers in tiered.items():
        values = tiers[min(tiers)]
        distinct = set(values)
        if len(distinct) == 1:
            result[field] = values[0]
            if (field, values[0]) not in proven:
                weak.add(field)
    if "currency" not in tiered and (code := _only_distinct(printed)) is not None:
        result["currency"] = code
    credits = {
        value for _, row in split for index, word in enumerate(row[1:], 1)
        if word.text.upper().strip(".") in {"CR", "DR"} for value in _row_numbers(row[index - 1:index])
    }
    for field in ("net", "tax"):
        if result.get(field) in credits:
            del result[field]
    result["credits"] = credits
    result["weak"] = weak
    return result
