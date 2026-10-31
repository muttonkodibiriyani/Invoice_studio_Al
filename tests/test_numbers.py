"""The one number parser every local reader uses: printed amounts in, plain decimal strings out."""
from decimal import Decimal

import pytest

from app.numbers import parse_number, to_decimal


@pytest.mark.parametrize("printed, plain", [
    ("1,067.13", "1067.13"),
    ("1,234,567.890", "1234567.890"),
    ("12.500", "12.500"),  # KWD fils keep their three places
    ("1,234.567", "1234.567"),
    ("9.00", "9.00"),
    ("800", "800"),
    ("950.40 AED", "950.40"),
    ("AED 950.40", "950.40"),
    ("12.500 KWD", "12.500"),
    ("KD12.500", "12.500"),
    ("Dhs. 5.00", "5.00"),
    ("100.00 AED", "100.00"),
    ("(12.50)", "-12.50"),
    ("(950.40) AED", "-950.40"),
    ("AED (1,000.00)", "-1000.00"),
    ("12.50-", "-12.50"),
    ("-3", "-3"),
    ("−12.50", "-12.50"),
    ("-0.00", "0.00"),
    ("1O0.00", "100.00"),
    ("10o.5O", "100.50"),
    ("1 , 105.31", "1105.31"),
    ("950. 40", "950.40"),
    ("8 800.00", "8800.00"),
    ("2.480.00", "2480.00"),  # a scan of 2,480.00
    ("1.234.567", "1234567"),
    ("1.067,13", "1067.13"),
    ("950,40", "950.40"),
    ("1,067", "1067"),
    ("١٬٢٣٤٫٥٠", "1234.50"),
    (" 1,067.13 ", "1067.13"),
    (40.0, "40.0"),
    (Decimal("9.00"), "9.00"),
    (7, "7"),
])
def test_printed_number_is_plain(printed, plain):
    assert parse_number(printed) == plain


@pytest.mark.parametrize("printed", [
    None, "", "-", "(", "O", "oo", "abc", "5%", "12 3.50", "12,3456", "12.34.56", "1,234.567,89", "1234,567",
    ".50", "1.2.3", True, float("nan"), "9" * 25,
])
def test_not_a_number_is_none(printed):
    assert parse_number(printed) is None


def test_to_decimal():
    assert to_decimal("1,067.13") == Decimal("1067.13")
    assert to_decimal("n/a") is None


def test_layout_reader_reads_no_negative_amount():
    from app.layout_extract import _parse_decimal

    assert _parse_decimal("1,067.13 AED") == Decimal("1067.13")
    assert _parse_decimal("(12.50)") is None
