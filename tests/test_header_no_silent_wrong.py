"""A header field is read correctly or left blank; a wrong value that looks fine is the one outcome never allowed.

Every value here is made up. The shapes are general ones: thousands printed with a space or an apostrophe, a strip of
headings over a strip of figures, a due date beside a "Dated:" label, labels that run along one line, a value printed
before its label, and wordings no supplier template knows."""
from collections import defaultdict
from decimal import Decimal

import pytest

from app.layout_extract import extract_invoice
from app.numbers import parse_number

WIDTH, HEIGHT = 1000.0, 1400.0


def boxes(*rows):
    """(y, (x, "words"), ...) rows as a text layer's word boxes: one space between the words of each phrase."""
    out = []
    for y, *phrases in rows:
        for x, phrase in phrases:
            for word in phrase.split():
                right = x + 6.5 * len(word)
                out.append({"text": word, "page": 1, "box": [x, y, right, y + 12], "size": [WIDTH, HEIGHT]})
                x = right + 3.5
    return out


def text(rows):
    return "\n".join(" ".join(phrase for _, phrase in phrases) for _, *phrases in rows)


TITLE = (60, (400, "Tax Invoice"))


def read(*rows):
    return defaultdict(lambda: None, extract_invoice(text((TITLE, *rows)), boxes(TITLE, *rows)) or {})


def money(value):
    return None if value is None else Decimal(str(value))


@pytest.mark.parametrize("printed, plain", [
    ("29'566.32", "29566.32"), ("1'478.32", "1478.32"), ("1’234’567.50", "1234567.50"), ("€1'250.00", "1250.00"),
    ("£950.40", "950.40"), ("$12.50", "12.50"),
])
def test_an_apostrophe_groups_thousands_and_a_sign_is_a_currency(printed, plain):
    assert parse_number(printed) == plain


@pytest.mark.parametrize("printed", ["12'34.50", "1'2", "12'", "'123"])
def test_an_apostrophe_out_of_place_is_not_a_number(printed):
    assert parse_number(printed) is None


@pytest.mark.parametrize("row, net", [
    ((900, (500, "Total (ex VAT)"), (760, "412 386,75")), "412386.75"),
    ((900, (500, "Subtotal"), (760, "2 104 907,10")), "2104907.10"),
    ((900, (500, "Net Amount"), (760, "83'210.45")), "83210.45"),
    ((900, (500, "Sub Total"), (760, "€3 118 204,06")), "3118204.06"),
])
def test_a_net_with_spaced_or_apostrophe_thousands_reads_whole(row, net):
    assert money(read(row)["net"]) == money(net)


def test_a_tax_with_spaced_thousands_is_not_a_rate_and_a_remainder():
    invoice = read((900, (500, "Tax"), (760, "33 825,39")))
    assert money(invoice["tax"]) == money("33825.39")


def test_a_vat_summary_row_with_apostrophes_reads_each_figure_whole():
    invoice = read(
        (880, (300, "Rate"), (420, "Taxable"), (560, "VAT"), (700, "Total")),
        (900, (300, "5%"), (420, "41'230.60"), (560, "2'061.53"), (700, "43'292.13")),
    )
    assert invoice["net"] in (None, "41230.60", 41230.60)
    assert money(invoice["net"]) != money("230.60")
    assert money(invoice["tax"]) != money("061.53")


def test_columns_of_figures_are_never_joined_as_thousands():
    invoice = read((900, (500, "Subtotal"), (700, "120"), (800, "450.00")))
    assert money(invoice["net"]) in (None, money("450.00"))
    assert money(invoice["net"]) != money("120450.00")


def test_a_strip_of_headings_over_figures_reads_each_under_its_own_heading():
    invoice = read(
        (880, (300, "Net Amount"), (500, "TVA 15 %"), (700, "Invoice Total")),
        (900, (300, "KWD 512,804.250"), (500, "KWD 76,920.638"), (700, "KWD 589,724.888")),
    )
    assert money(invoice["net"]) == money("512804.250")
    assert money(invoice["tax"]) == money("76920.638")


def test_a_rate_in_a_heading_is_never_the_net():
    invoice = read((880, (300, "Net Amount"), (500, "TVA 15 %"), (700, "Invoice Total")))
    assert invoice["net"] is None
    assert invoice["tax"] is None


def test_a_due_date_is_not_the_invoice_date():
    invoice = read(
        (100, (70, "Delivery Note No: DN-44120"), (500, "Dated: 13/03/2026")),
        (120, (70, "Client PO: LPO/7731"), (500, "Due Date: 25/04/2026")),
    )
    assert invoice["date"] == "2026-03-13"


@pytest.mark.parametrize("label", ["Due Date", "Order Date", "Delivery Date", "Payment Date", "Ship Date", "PO Date"])
def test_only_a_qualified_date_on_the_page_leaves_the_date_blank(label):
    assert read((100, (70, f"{label}: 25/04/2026")))["date"] is None


@pytest.mark.parametrize("row, number, date, po", [
    ("Invoice No.: QT/2026/81234 Date of Issue: 19-05-2026", "QT/2026/81234", "2026-05-19", None),
    ("Tax Invoice No Z902117 Invoice Date 14/05/2026 Your Order LPO/5521", "Z902117", "2026-05-14", "LPO/5521"),
    ("Folio: K772041 Issue Date: 2026-08-19 LPO: LPO/2288 Ccy: QAR", "K772041", "2026-08-19", "LPO/2288"),
])
def test_a_number_stops_at_the_next_label(row, number, date, po):
    invoice = read((100, (70, row)))
    assert invoice["number"] == number
    assert invoice["date"] == date
    assert invoice["po"] == po


def test_a_value_printed_before_its_label_belongs_to_that_label():
    invoice = read(
        (100, (70, "K505188 : Tax Invoice No 24.01.2026 : Dated")),
        (120, (70, "99021 : Purchase Order")),
        (140, (70, "OMR : Invoice Currency")),
    )
    assert invoice["number"] == "K505188"
    assert invoice["date"] == "2026-01-24"
    assert invoice["po"] == "99021"
    assert invoice["currency"] == "OMR"


def test_a_date_is_never_an_invoice_number():
    assert read((100, (70, "Invoice No 24.01.2026")))["number"] is None
    assert read((100, (70, "Invoice No: 2026-01-24")))["number"] is None


def test_a_heading_strip_reads_identifiers_and_dates_under_their_headings():
    invoice = read(
        (100, (70, "Bill Number"), (300, "Date of Supply"), (520, "Client PO")),
        (120, (70, "H566311"), (300, "03.03.2026"), (520, "41890")),
    )
    assert invoice["number"] == "H566311"
    assert invoice["date"] == "2026-03-03"
    assert invoice["po"] == "41890"


def test_a_near_label_is_not_the_field():
    invoice = read(
        (100, (70, "Delivery Note No: DN-55102"), (500, "Order No: 617005")),
        (120, (70, "Reference Invoice No: OLD-8812"), (500, "Account No: 4411")),
    )
    assert invoice["number"] is None
    assert invoice["po"] is None


def test_two_invoice_numbers_leave_the_number_blank():
    invoice = read((100, (70, "Bill No: A-1001")), (120, (70, "Bill No: A-1002")))
    assert invoice["number"] is None


def test_last_invoices_subtotal_in_a_sentence_is_not_the_net():
    invoice = read(
        (880, (70, "Last invoice Sub Total: 913351.86 Rate 16%")),
        (900, (500, "Goods Value"), (760, "1,200.00")),
    )
    assert money(invoice["net"]) == money("1200.00")


@pytest.mark.parametrize("row", ["TOTAL DUE 1,050.00", "Total VAT 50.00", "Currency: DUE"])
def test_a_word_of_three_letters_is_not_a_currency(row):
    assert read((900, (500, row)))["currency"] is None


@pytest.mark.parametrize("row, currency", [
    ("Currency: EGP", "EGP"), ("Ccy: JPY", "JPY"), ("All amounts in Bahraini Dinars", "BHD"),
    ("Net (EUR) 1,200.00", "EUR"),
])
def test_a_currency_is_read_from_what_the_page_says(row, currency):
    assert read((900, (500, row)))["currency"] == currency


@pytest.mark.parametrize("name", [
    "Marigold Foods Pvt Ltd", "Marigold Foods Private Limited", "Marigold Foods Pte Ltd", "Marigold Trading Co. Ltd",
    "Marigold Trading Est.", "Marigold Foods S.A.", "Marigold Foods Pvt",
])
def test_a_letterhead_keeps_its_whole_company_form(name):
    invoice = read((30, (70, f"{name} TRN 100200300400003")))
    assert invoice["supplier_name"] == name


def test_a_buyer_at_the_top_of_the_page_is_not_the_letterhead():
    invoice = read((30, (70, "Bill To: Marigold Foods Ltd")))
    assert invoice["supplier_name"] is None
    assert invoice["buyer_name"] == "Marigold Foods Ltd"


@pytest.mark.parametrize("label", ["Issued by", "Supplied by", "Billed from", "Supplier"])
def test_a_supplier_label_reads_the_name_and_not_the_label(label):
    invoice = read((100, (70, f"{label}: Corvid Paper Mills")))
    assert invoice["supplier_name"] == "Corvid Paper Mills"


@pytest.mark.parametrize("title", ["VAT Invoice", "Bill", "CREDIT NOTE", "Credit Memo", "Debit Note"])
def test_a_title_no_template_knows_still_opens_the_layout_reader(title):
    head = (60, (400, title))
    rows = (head, (100, (70, "Folio: K772041")), (120, (70, "Issue Date: 2026-08-19")))
    invoice = extract_invoice(text(rows), boxes(*rows))
    assert invoice["number"] == "K772041"
    assert invoice["date"] == "2026-08-19"


@pytest.mark.parametrize("title", ["PURCHASE ORDER", "Packing List", "Statement of Account"])
def test_other_documents_stay_closed(title):
    rows = ((60, (400, title)), (100, (70, "Order Ref: 4471")))
    assert extract_invoice(text(rows), boxes(*rows)) is None


def test_a_page_labelled_in_arabic_reads_each_value_beside_its_label():
    rows = (
        (60, (400, "فاتورة ضريبية")),
        (100, (70, "K505188 : رقم الفاتورة")),
        (120, (70, "2026-02-19 : تاريخ الفاتورة")),
        (140, (70, "99021 : أمر الشراء")),
        (900, (70, "١٢٬٣٤٠٫٥٠٠ المجموع الفرعي")),
        (920, (70, "٦١٧٫٠٢٥ ضريبة القيمة المضافة")),
    )
    invoice = extract_invoice(text(rows), boxes(*rows))
    assert invoice["number"] == "K505188"
    assert invoice["date"] == "2026-02-19"
    assert invoice["po"] == "99021"
    assert money(invoice["net"]) == money("12340.500")
    assert money(invoice["tax"]) == money("617.025")


def test_arabic_beside_an_english_label_only_repeats_it():
    invoice = read((100, (70, "Bill Number رقم الفاتورة K505188"), (500, "Doc. Date تاريخ الفاتورة May 15, 2026")))
    assert invoice["number"] == "K505188"
    assert invoice["date"] == "2026-05-15"


def test_a_credit_note_leaves_its_amounts_to_review_when_it_prints_no_sign():
    rows = ((60, (400, "CREDIT NOTE")), (100, (70, "Credit Note No: CN-5512")), (900, (70, "Subtotal 1,200.00")),
            (920, (70, "VAT 60.00")))
    invoice = extract_invoice(text(rows), boxes(*rows))
    assert invoice["number"] == "CN-5512"
    assert invoice["net"] is None
    assert invoice["tax"] is None


def test_a_figure_marked_cr_is_left_to_review():
    invoice = read((900, (70, "Merchandise Total GBP 1,200.00 CR")))
    assert invoice["net"] is None


def test_a_conversion_line_is_not_the_invoice_currency():
    invoice = read((900, (70, "Total Amount 1,200.50 EGP")), (920, (70, "Total equivalent in EUR 610.25")))
    assert invoice["currency"] == "EGP"


def test_lakh_grouping_reads_whole():
    assert parse_number("12,34,567.89") == "1234567.89"
    assert money(read((900, (70, "Taxable Value 12,34,567.89")))["net"]) == money("1234567.89")


@pytest.mark.parametrize("row, field, value", [
    ("Total Taxable Supplies€812,007.27", "net", "812007.27"), ("Net Value809102.127", "net", "809102.127"),
    ("VAT @ 5%97206.699 KWD", "tax", "97206.699"), ("Value Excluding TaxAED 1,664,572.21", "net", "1664572.21"),
    ("Goods Value BD2,578,406.085", "net", "2578406.085"), ("Net 204,481,405", "net", "204481405"),
])
def test_a_label_run_into_its_figure_still_reads(row, field, value):
    assert money(read((900, (70, row)))[field]) == money(value)


@pytest.mark.parametrize("row, field", [("VAT 5", "tax"), ("Tax 2099", "tax"), ("Net 30", "net")])
def test_a_small_whole_number_after_a_label_is_no_amount(row, field):
    assert read((900, (70, row)))[field] is None


def test_an_arabic_wording_that_only_ends_in_tax_is_not_the_tax():
    invoice = read((900, (70, "1,200.00 : مبلغ ما قبل الضريبة")))
    assert invoice["tax"] is None


def test_a_tax_summary_with_one_rate_reads_under_its_columns():
    found = read(
        (200, (300, "Rate"), (360, "Taxable"), (440, "VAT"), (500, "Total")),
        (212, (300, "20%"), (345, "1,250.500"), (425, "250.100"), (490, "1,500.600")),
        (240, (300, "Invoice Total"), (400, "1,500.600")),
    )
    assert (found["net"], found["tax"]) == ("1250.500", "250.100")


def test_a_tax_summary_with_two_rates_is_not_read_as_one():
    found = read(
        (200, (300, "Rate"), (360, "Taxable"), (440, "VAT"), (500, "Total")),
        (212, (300, "20%"), (345, "1,250.500"), (425, "250.100"), (490, "1,500.600")),
        (224, (300, "5%"), (345, "100.000"), (425, "5.000"), (490, "105.000")),
    )
    assert found["net"] is None and found["tax"] is None


def test_a_totals_strip_with_a_code_on_each_figure_reads_each():
    found = read(
        (200, (45, "Net Amount"), (215, "VAT 15%"), (386, "Invoice Total")),
        (212, (45, "OMR 12,500.125"), (215, "OMR 1,875.019"), (386, "OMR 14,375.144")),
        (240, (45, "Item Code"), (160, "Description"), (400, "Qty")),
    )
    assert (found["net"], found["tax"]) == ("12500.125", "1875.019")


def test_a_one_line_table_is_no_totals_strip():
    found = read(
        (200, (45, "Description"), (215, "Net Amount"), (320, "VAT"), (400, "Total")),
        (212, (45, "Blue widget"), (215, "120.00"), (320, "6.00"), (400, "126.00")),
    )
    assert found["net"] is None and found["tax"] is None


def test_a_rate_in_brackets_with_its_own_closing_bracket_reads_the_figure():
    assert read((300, (60, "VAT Amount"), (120, "(5%"), (142, ")"), (150, ":"), (170, "45.50")))["tax"] == "45.50"


def test_a_total_on_the_far_side_of_a_repeated_arabic_label():
    assert read((300, (60, "Tax"), (90, "ضريبة القيمة المضافة"), (400, "QR812.500")))["tax"] == "812.500"


def test_an_arabic_amount_not_including_tax_is_the_net():
    assert read((300, (60, "1,250.00"), (140, "المبلغ غير شامل الضريبة")))["net"] == "1250.00"


def test_two_figures_run_together_stay_blank():
    found = read(
        (200, (300, "Rate"), (360, "Taxable"), (440, "VAT"), (500, "Total")),
        (212, (300, "20%"), (345, "1,250.50"), (425, "250.101,500.60")),
    )
    assert found["tax"] is None


def test_a_figure_cut_at_its_commas_is_read_whole():
    found = read(
        (300, (60, "KD1"), (82, ","), (90, "250"), (112, ","), (120, "500.125"), (300, "المجموع الفرعي")),
        (312, (60, "KD250"), (90, ","), (100, "100.025"), (300, "ضريبة القيمة المضافة")),
    )
    assert (found["net"], found["tax"]) == ("1250500.125", "250100.025")


def test_a_tax_above_its_net_leaves_both_blank():
    found = read((300, (60, "Subtotal"), (200, "1.50")), (312, (60, "VAT"), (200, "250.10")))
    assert found["net"] is None and found["tax"] is None


def test_a_po_box_is_no_purchase_order_however_a_scan_cuts_it():
    found = read(
        (100, (60, "P"), (68, "."), (76, "O"), (84, "."), (92, "Box7920"), (300, "Tel 5551234")),
        (120, (60, "P"), (68, "."), (76, "O"), (84, "."), (92, "No"), (110, ":"), (120, "LPO"), (146, "/"), (154, "923")),
    )
    assert found["po"] == "LPO/923"


@pytest.mark.parametrize("printed, po", [("P0-67956", "PO-67956"), ("LP0 / 6543", "LPO/6543"), ("P07788", "P07788")])
def test_a_zero_read_for_the_o_of_a_printed_po_word(printed, po):
    assert read((120, (60, "Your Ref :"), (140, printed)))["po"] == po


def test_thousands_after_a_printed_space_on_a_subtotal():
    assert read((300, (60, "Sub Total"), (130, "10 500.00")))["net"] == "10500.00"


@pytest.mark.parametrize("wording", [
    "Net Amount Due", "Net Total Due", "Net Amount Payable", "Net Amount incl. VAT", "Subtotal incl VAT",
    "Subtotal Gross", "Subtotal With VAT",
])
def test_a_gross_wording_is_no_net(wording):
    assert read((300, (60, f"{wording} 1,050.00")))["net"] is None
    assert read((300, (60, f"{wording} :"), (300, "1,050.00")))["net"] is None


@pytest.mark.parametrize("wording", ["Subtotal Qty 12", "Sub Total Items 12", "Subtotal Lines: 12", "Subtotal 2026"])
def test_a_count_or_a_year_is_no_net(wording):
    assert read((300, (60, wording)))["net"] is None


def test_a_net_stands_when_a_plain_wording_prints_the_same_figure():
    found = read((280, (60, "Subtotal 1,050.00")), (300, (60, "Total Due 1,050.00")))
    assert found["net"] == "1050.00"


@pytest.mark.parametrize("wording", [
    "VAT 33 825.39", "Tax Amount Due 4,725.00", "Withholding Tax Amount 50.00", "Service Tax Amount 50.00",
    "Tourism Tax Amount 50.00", "VAT 12 Mar 2026",
])
def test_a_figure_that_is_no_tax_of_this_invoice(wording):
    assert read((300, (60, wording)))["tax"] is None


def test_a_spaced_tax_figure_reads_whole():
    assert read((300, (60, "Tax Amount 33 825.39")))["tax"] == "33825.39"


@pytest.mark.parametrize("wording", [
    "Foreign Currency : EUR", "Reporting Currency : EUR", "Beneficiary Currency : EUR", "Remit Currency: USD",
    "Bank A/C Currency : EUR",
])
def test_a_currency_named_for_something_else(wording):
    assert read((300, (60, wording)))["currency"] is None


def test_the_invoice_currency_stands_beside_a_reporting_one():
    found = read((280, (60, "Invoice Currency : USD")), (300, (60, "Reporting Currency : EUR")))
    assert found["currency"] == "USD"


def test_a_table_of_counts_is_no_totals():
    found = read(
        (280, (60, "Taxable Items"), (220, "VAT Items"), (380, "Total Items")),
        (300, (60, "2"), (220, "1"), (380, "3")),
    )
    assert found["net"] is None and found["tax"] is None


def edge(rows):
    """Word boxes where the last word of a row is cut off by the page's right edge."""
    out = boxes(*rows)
    return [
        {**word, "box": [word["box"][0], word["box"][1], WIDTH - 1, word["box"][3]]} if word["text"].endswith("~")
        else word for word in out
    ]


@pytest.mark.parametrize("row, field", [
    ((300, (60, "Sub Total :"), (560, "1126~")), "net"),
    ((300, (60, "VAT Amount :"), (560, "471~")), "tax"),
    ((100, (60, "Invoice No :"), (560, "SI/2026~")), "number"),
    ((120, (60, "PO No :"), (560, "2~")), "po"),
])
def test_a_value_the_page_edge_cuts_is_not_read(row, field):
    words = [{**word, "text": word["text"].rstrip("~")} for word in edge([row])]
    assert (extract_invoice(" ".join(word["text"] for word in words), words) or {}).get(field) is None


def test_a_supplier_the_page_edge_cuts_is_not_read():
    words = boxes((60, (2, "lvaneHardware GmbH")), (100, (60, "Invoice No : 4471")))
    assert (extract_invoice("\n".join(["lvaneHardware GmbH", "Invoice No : 4471"]), words) or {}).get("supplier_name") is None


def test_a_label_glues_to_a_hyphenated_value():
    assert read((100, (60, "PO NumberPO-49791")))["po"] == "PO-49791"


@pytest.mark.parametrize("lead", ["Non-taxable", "Exempt", "غير", "معفى"])
def test_a_negating_lead_is_no_taxable_amount(lead):
    assert read((800, (100, f"{lead} Taxable Amount 50.00")))["net"] is None


def test_a_gross_only_row_is_no_net_when_the_grand_total_repeats_it():
    invoice = read((800, (100, "Total Taxable Amount Incl VAT 4,725.00")), (900, (100, "Grand Total"), (700, "4,725.00")))
    assert invoice["net"] is None


COLUMNS = (300, 500, 700)


def summary(*head, values=("9,000.00", "450.00", "9,450.00")):
    return (700, *zip(COLUMNS, head)), (720, *zip(COLUMNS, values))


@pytest.mark.parametrize("rows", [
    summary("YTD Taxable", "YTD VAT", "YTD Total"),
    summary("Previous Taxable", "Previous VAT", "Previous Total"),
    ((680, (300, "Prior Invoice")), *summary("Taxable", "VAT", "Total")),
    ((680, (300, "Year to date")), *summary("Taxable", "VAT", "Total")),
    ((680, (300, "Credit Memo")), *summary("Taxable", "VAT", "Total")),
    ((700, (100, "Prior Invoice"), (300, "Taxable"), (500, "VAT"), (700, "Total")),
     (720, (300, "9,000.00"), (500, "450.00"), (700, "9,450.00"))),
    ((680, (300, "Non")), *summary("Taxable", "VAT", "Total")),
    ((680, (300, "Exempt")), *summary("Taxable", "VAT", "Total")),
])
def test_a_summary_table_of_another_period_or_kind_is_not_this_invoice(rows):
    invoice = read(*rows)
    assert invoice["net"] is None and invoice["tax"] is None


def test_a_word_above_another_column_or_a_plain_heading_does_not_stop_the_summary():
    invoice = read((680, (100, "Prior balance nil")), *summary("Taxable", "VAT", "Total"))
    assert money(invoice["net"]) == Decimal("9000.00") and money(invoice["tax"]) == Decimal("450.00")


def paged(page, *rows):
    return [{**box, "page": page} for box in boxes(*rows)]


def read_pages(first, second):
    rows = [TITLE, *first, *second]
    return defaultdict(lambda: None, extract_invoice(text(rows), paged(1, TITLE, *first) + paged(2, *second)) or {})


@pytest.mark.parametrize("words", [
    "Tel +971 4 123 4567", "Dated 12 03 2026", "Form version 1.2", "Clause 3.1", "Fee 2.5 percent", "Weight 12.5 kg",
])
def test_digits_that_are_no_money_on_a_later_page_leave_the_summary(words):
    invoice = read_pages(summary("Taxable", "VAT", "Total"), [(100, (100, words))])
    assert money(invoice["net"]) == Decimal("9000.00") and money(invoice["tax"]) == Decimal("450.00")


@pytest.mark.parametrize("second", [
    [(100, (100, "Grand Total"), (700, "4,725.00"))],
    [(100, (300, "1,000.000"), (500, "50.000"))],
    [(100, (700, "Taxable"), (800, "VAT"), (900, "Total"))],
])
def test_money_or_a_moved_heading_on_a_later_page_still_blanks_the_summary(second):
    invoice = read_pages(summary("Taxable", "VAT", "Total"), second)
    assert invoice["net"] is None and invoice["tax"] is None


def test_a_summary_printed_whole_still_refuses_whole_figures_on_a_later_page():
    invoice = read_pages(summary("Taxable", "VAT", "Total", values=("4500", "225", "4725")), [(100, (100, "1000 50 1050"))])
    assert invoice["net"] is None and invoice["tax"] is None


def test_a_bare_colon_after_the_label_still_reads():
    assert money(read((800, (100, "Subtotal : 4,500.00")))["net"]) == Decimal("4500.00")
