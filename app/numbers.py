"""The one parser for a printed number: every local reader turns printed text into an amount through here.

parse_number("1,067.13") == "1067.13"; it returns a plain decimal string with the printed places kept
("9.00" stays "9.00", a KWD "12.500" stays "12.500"), or None when the text is not one number. Read:
- thousands separators: "1,067.13", "1,234,567", a space "8 800.00", an apostrophe "29'566.32", lakhs "12,34,567.89", and
  the other convention "1.067,13";
  with one kind of separator printed twice or more, the last one is the decimal point unless its group has
  three digits ("2.480.00" is a scan of "2,480.00"; "1.234.567" is 1234567);
- one comma: three digits after it group thousands ("1,067"); one or two are the decimal point ("950,40");
- a currency code or sign before or after the amount, with or without a space ("950.40 AED", "KD12.500", "€950.40");
- negatives: "-12.50", "(12.50)", "12.50-", and the Unicode minus;
- OCR noise: O or o for a zero among digits ("1O0.00"), spaces around a separator ("1 , 105.31", "950. 40"),
  Arabic-Indic digits and the Arabic decimal and thousands marks.
Anything else (a percent, two numbers in one cell, letters, a lone "O") is not a number: None, never a guess.
"""
import re
from decimal import Decimal

# ISO codes printed on Gulf invoices, and the local signs suppliers print instead.
_CURRENCY = r"(?:AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR|INR|Dhs?|KD|SR|QR|BD|RO|Rs|[€£$])\.?"
_AROUND = re.compile(rf"(?i)^(?:{_CURRENCY}\s*)?(?P<number>.*?)(?:\s*{_CURRENCY})?$")
_ARABIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬−   ",
                        "01234567890123456789.,-   ")
_SHAPE = re.compile(r"[0-9]+(?:[.,][0-9]+)*")
_MAX_DIGITS = 24
# The codes a printed word may be read as: "DUE" and "VAT" have three capitals and are no currency.
ISO_CURRENCIES = frozenset(
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF "
    "CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG "
    "HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA "
    "MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD "
    "RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX "
    "USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWL".split()
)


def parse_number(value):
    """The printed number as a plain decimal string, or None when the text is not one number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return _plain(Decimal(str(value)))
    text = str(value).translate(_ARABIC).strip()
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1].strip()
    text = _AROUND.fullmatch(text).group("number")
    if text.startswith("(") and text.endswith(")") and not negative:  # "AED (12.50)"
        negative, text = True, text[1:-1].strip()
    if text.startswith(("+", "-")):
        negative, text = negative or text[0] == "-", text[1:].strip()
    elif text.endswith("-"):
        negative, text = True, text[:-1].strip()
    if re.search(r"[0-9]", text):
        text = re.sub(r"[Oo]", "0", text)
    text = re.sub(r"\s*([.,])\s*", r"\1", text)
    if re.fullmatch(r"[0-9]{1,3}(?: [0-9]{3})+(?:[.,][0-9]+)?", text):
        text = text.replace(" ", "")
    if re.fullmatch(r"[0-9]{1,3}(?:['’][0-9]{3})+(?:[.,][0-9]+)?", text):
        text = re.sub(r"['’]", "", text)
    if re.fullmatch(r"[0-9]{1,2}(?:,[0-9]{2})+,[0-9]{3}(?:\.[0-9]+)?", text):  # Indian lakh grouping: "12,34,567.89"
        text = text.replace(",", "")
    if not _SHAPE.fullmatch(text):
        return None
    plain = _ungroup(text)
    if plain is None:
        return None
    number = Decimal(plain)
    if len(number.as_tuple().digits) > _MAX_DIGITS:
        return None
    return _plain(-number if negative else number)


def to_decimal(value):
    """parse_number as a Decimal, or None."""
    plain = parse_number(value)
    return None if plain is None else Decimal(plain)


def _ungroup(text):
    """Digits and separators as a plain number: which separators group thousands and which one is the point."""
    separators = re.findall(r"[.,]", text)
    groups = re.split(r"[.,]", text)
    if not separators:
        return text
    if len(separators) == 1:
        if separators[0] == "." or len(groups[1]) in (1, 2):
            return groups[0] + "." + groups[1]
        if len(groups[1]) == 3 and len(groups[0]) <= 3:
            return groups[0] + groups[1]
        return None
    if len(groups[0]) > 3 or any(len(group) != 3 for group in groups[1:-1]):
        return None
    grouping = set(separators[:-1])
    if len(grouping) != 1:
        return None
    if separators[-1] not in grouping or len(groups[-1]) != 3:
        return "".join(groups[:-1]) + "." + groups[-1]
    return "".join(groups)


def _plain(number):
    if not number.is_finite():
        return None
    return format(number if number else abs(number), "f")  # "-0.00" is "0.00"
