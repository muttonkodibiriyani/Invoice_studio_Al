"""Header fields read from what the page prints, on layouts no supplier template knows.

Every invoice here is synthetic. The shapes come from a scan-vs-truth run on real invoices: an ERP export that prints
"Total Untaxed Amount" and "VAT" with no rate, a figure with its currency code glued on ("1,234.50AED"), the currency
as a value under a "Currency" column heading, and a letterhead name with no company form that the footer repeats."""
from decimal import Decimal

import pytest

from app import ocr_worker
from app import target_check as tc
from app.layout_extract import extract_invoice

WIDTH, HEIGHT = 1000.0, 1400.0


def boxes(*rows):
    """(y, (x, word), ...) rows as a reader's word boxes; a word is about 9 units wide per character."""
    out = []
    for y, *words in rows:
        for x, word in words:
            out.append({"text": word, "page": 1, "box": [x, y, x + 9 * len(word), y + 14], "size": [WIDTH, HEIGHT]})
    return out


def text(rows):
    return "\n".join(" ".join(word for _, word in words) for _, *words in rows)


def read(*rows):
    return extract_invoice(text(rows), boxes(*rows))


TITLE = (220, (400, "Tax"), (440, "Invoice"))
NUMBER = (110, (680, "Doc"), (720, "No"), (745, ":"), (800, "ZZ123456"))
HEADING = (480, (80, "Barcode"), (170, "Description"), (580, "Qty"), (630, "Price"), (690, "Value"), (860, "Total"))
ROW = (510, (80, "6291041500213"), (170, "Blue"), (220, "pen"), (580, "10"), (630, "450.00"), (690, "4,500.00"),
       (860, "4,725.00"))


def erp_export(net=(("4", 850), (",", 862), ("500.00", 870)), tax=(("225.00", 870),)):
    return (
        (110, (70, "Harbour"), (240, "Lane"), *NUMBER[1:]),
        TITLE,
        (340, (70, "Bill"), (110, "To"), (140, ":")),
        (360, (80, "Retail"), (150, "Stores"), (230, "LLC")),
        (440, (670, "Currency"), (780, "Purchase"), (840, "Order")),
        (465, (690, "AED"), (800, "70801")),
        HEADING, ROW,
        (950, (530, "Total"), (570, "Untaxed"), (630, "Amount"), *((x, w) for w, x in net)),
        (980, (530, "VAT"), *((x, w) for w, x in tax)),
        (1010, (530, "Total"), (570, "Incl."), (610, "VAT"), (850, "4"), (862, ","), (870, "725.00")),
        (1230, (90, "Harbour"), (180, "Lane")),
        (1250, (90, "12"), (120, "Quay"), (170, "Road")),
    )


def test_an_erp_export_reads_its_labelled_header_fields():
    invoice = read(*erp_export())
    assert invoice["number"] == "ZZ123456"
    assert invoice["supplier_name"] == "Harbour Lane"
    assert invoice["currency"] == "AED"
    assert Decimal(invoice["net"]) == Decimal("4500.00")
    assert Decimal(invoice["tax"]) == Decimal("225.00")


@pytest.mark.parametrize("label", [
    ("Untaxed", "Amount"), ("Taxable", "Amount"), ("Taxable", "Value"), ("Total", "before", "VAT"),
    ("Amount", "before", "Tax"), ("Total", "Excl.", "VAT")])
def test_each_printed_wording_of_the_net_is_read(label):
    row = (950, *((530 + 70 * n, word) for n, word in enumerate(label)), (870, "4,500.00"))
    assert Decimal(read(TITLE, NUMBER, row)["net"]) == Decimal("4500.00")


@pytest.mark.parametrize("words", [
    ("Total", "4,725.00"), ("Total", "lines", "12.00"), ("Total", "Incl.", "VAT", "4,725.00"),
    ("Grand", "Total", "4,725.00"), ("Amount", "Due", "4,725.00")])
def test_a_gross_or_a_line_count_is_never_read_as_the_net(words):
    row = (950, *((530 + 70 * n, word) for n, word in enumerate(words)))
    assert read(TITLE, NUMBER, row)["net"] is None


def test_the_net_stops_at_the_next_label_on_its_row():
    row = (950, (100, "Taxable"), (170, "Amount"), (260, "4,500.00"), (400, "VAT"), (450, "225.00"))
    assert Decimal(read(TITLE, NUMBER, row)["net"]) == Decimal("4500.00")


def test_net_and_tax_with_the_code_glued_to_the_figure():
    rows = (TITLE, NUMBER,
            (950, (80, "Payment"), (160, "Communication:"), (300, "ZZ123456"), (530, "Untaxed"), (600, "Amount"),
             (850, "1"), (862, ","), (870, "234.50AED")),
            (980, (530, "VAT"), (580, "5"), (590, "%"), (870, "61.73AED")),
            (1010, (530, "Total"), (850, "1"), (862, ","), (870, "296.23AED")))
    invoice = read(*rows)
    assert Decimal(invoice["net"]) == Decimal("1234.50")
    assert Decimal(invoice["tax"]) == Decimal("61.73")
    assert invoice["currency"] == "AED"


def test_a_zero_value_invoice_reads_zero_not_the_rate():
    rows = (TITLE, NUMBER, (950, (530, "Untaxed"), (600, "Amount"), (870, "0.00AED")),
            (980, (530, "VAT5%"), (870, "0.00AED")), (1010, (530, "Total"), (870, "0.00AED")))
    invoice = read(*rows)
    assert Decimal(invoice["net"]) == 0 and Decimal(invoice["tax"]) == 0


@pytest.mark.parametrize("row, expected", [
    ("VAT 456.78", "456.78"), ("VAT 1,234.56", "1234.56"), ("VAT 5% 456.78", "456.78"), ("VAT 5 456.78", "456.78"),
    ("VAT 5% AED 456.78", "456.78"), ("VAT 5% 456.78AED", "456.78"), ("Tax: 5% 12.00", "12.00")])
def test_a_tax_row_is_never_split_inside_its_amount(row, expected):
    invoice = extract_invoice(f"Tax Invoice\nInvoice No: Z1\n{row}\n", None)
    assert Decimal(invoice["tax"]) == Decimal(expected)


@pytest.mark.parametrize("row", ["VAT 5%", "VAT 15", "VAT 5"])
def test_a_printed_rate_alone_is_not_the_tax(row):
    assert extract_invoice(f"Tax Invoice\nInvoice No: Z1\n{row}\n", None)["tax"] is None


def test_text_with_no_word_boxes_reads_the_same_labels():
    invoice = extract_invoice("Tax Invoice\nInvoice No: Z1\nTotal Untaxed Amount 4,500.00\nVAT 225.00\n", None)
    assert Decimal(invoice["net"]) == Decimal("4500.00")
    assert Decimal(invoice["tax"]) == Decimal("225.00")


def currency_of(heading, value):
    return read(TITLE, NUMBER, (440, heading), (465, value))["currency"]


def test_a_currency_under_its_column_heading_is_read():
    assert currency_of((670, "Currency"), (690, "AED")) == "AED"
    assert read(TITLE, NUMBER, (440, (670, "Currency"), (760, "SAR")))["currency"] == "SAR"


def test_only_a_known_code_directly_under_the_heading_is_a_currency():
    assert currency_of((670, "Currency"), (690, "NET")) is None
    assert currency_of((670, "Currency"), (120, "AED")) is None
    assert currency_of((670, "Purchase"), (690, "AED")) is None


def test_two_printed_currencies_leave_it_blank():
    rows = (TITLE, NUMBER, (440, (670, "Currency")), (465, (690, "AED")), (800, (670, "Currency")), (825, (690, "USD")))
    assert read(*rows)["currency"] is None


def letterhead(top, bottom):
    return read((110, *top, *NUMBER[1:]), TITLE, (1230, *bottom), (1250, (90, "12"), (120, "Quay"), (170, "Road")))


def test_a_letterhead_name_repeated_in_the_footer_is_the_supplier():
    assert letterhead(((70, "Harbour"), (240, "Lane")), ((90, "Harbour"), (180, "Lane")))["supplier_name"] == "Harbour Lane"


def test_a_letterhead_name_the_footer_does_not_repeat_stays_blank():
    assert letterhead(((70, "Harbour"), (240, "Lane")), ((90, "Terms"), (180, "Apply")))["supplier_name"] is None


def test_a_repeated_title_or_buyer_is_not_the_supplier():
    assert letterhead(((70, "Tax"), (240, "Invoice")), ((90, "Tax"), (180, "Invoice")))["supplier_name"] is None
    rows = ((110, (70, "Retail"), (240, "Stores"), *NUMBER[1:]), TITLE, (340, (70, "Bill"), (110, "To"), (140, ":")),
            (360, (80, "Retail"), (150, "Stores")), (1230, (90, "Retail"), (180, "Stores")))
    assert read(*rows)["supplier_name"] is None


def test_a_printed_supplier_label_still_wins_over_the_repeated_letterhead():
    rows = ((110, (70, "Harbour"), (240, "Lane"), *NUMBER[1:]), TITLE,
            (300, (70, "Supplier:"), (170, "Quay"), (230, "Traders"), (300, "LLC")), (1230, (90, "Harbour"), (180, "Lane")))
    assert read(*rows)["supplier_name"] == "Quay Traders LLC"


@pytest.mark.parametrize("read_as, restored", [
    ("Sl/2099/0007", "SI/2099/0007"), ("SlA/2099/00009", "SIA/2099/00009"), ("SI/2099/0007", "SI/2099/0007"),
    ("Inv0007", "Inv0007"), ("Invl0007", "Invl0007"), ("l0007", "l0007"), ("0007-Sl", "0007-Sl"), (None, None)])
def test_ocr_restores_only_a_lower_case_l_among_capitals(read_as, restored):
    assert ocr_worker.restore_capital_i(read_as) == restored


def view_of(number):
    field = {"value": number, "evidence": [{"source": "Invoice page 1", "reference": "page 1 box 2", "original": number}]}
    return {"fields": {"number": field}, "lines": [], "issues": []}


def document_status(number, printed):
    result = tc.check_view(view_of(number), tc.Sources(config={}, text=printed))
    return next(c for c in result["cells"] if (c["sheet"], c["column"]) == ("Header", "Document"))["status"]


def test_a_restored_number_is_verified_only_where_the_page_also_prints_it():
    assert document_status("SI/2099/0007", "Tax Invoice Sl/2099/0007") != tc.VERIFIED
    assert document_status("SI/2099/0007", "Tax Invoice Sl/2099/0007\nPayment Communication: SI/2099/0007") == tc.VERIFIED


def tables_read(*rows):
    return ocr_worker.structured_extract(text(rows), boxes(*rows), [], [])[0]


def table_row(y, *amounts):
    return (y, (80, "6291041500213"), (170, "Blue"), (560, "1.00"), *amounts)


TABLE_HEADING = (480, (80, "Barcode"), (170, "Description"), (550, "Quantity"), (640, "Price"), (720, "Disc"),
                 (800, "VAT"), (880, "Amount"))
PLAIN = table_row(510, (640, "200.00"), (720, "10.00"), (800, "5%"), (880, "180.00"))


@pytest.mark.parametrize("x", [640, 690])
def test_a_row_whose_price_and_discount_ocr_glued_together_is_still_read(x):
    # The word centres under the price heading or the next one, by how much it swallowed.
    glued = table_row(540, (x, "200.0010.00VAT5"), (880, "180.00"))
    lines = tables_read(TITLE, NUMBER, TABLE_HEADING, PLAIN, glued)["lines"]
    assert [line["price"] for line in lines] == ["200.00", "200.00"]


def test_a_four_decimal_price_is_not_cut_in_two():
    row = table_row(510, (640, "120.5012"), (880, "120.50"))
    assert tables_read(TITLE, NUMBER, TABLE_HEADING, row)["lines"][0]["price"] == "120.5012"


@pytest.mark.parametrize("row, expected", [
    ((700, (600, "VAT"), (640, "Amount"), (690, "5%"), (870, "100.00")), "100.00"),
    ((700, (600, "VAT"), (870, "5")), None),
    ((700, (600, "VAT"), (640, "No"), (870, "100123456700003")), None),
])
def test_a_tax_row_never_reads_its_rate_or_a_registration_number(row, expected):
    tax = read(TITLE, NUMBER, row)["tax"]
    assert (None if tax is None else str(tax)) == expected
