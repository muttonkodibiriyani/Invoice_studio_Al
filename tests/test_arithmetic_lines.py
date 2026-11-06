"""Rows found by their arithmetic when no heading names the columns. Every figure and wording is invented.
A wrong row in the Details sheet is worse than no row, so most of these are things that must not become one."""
from decimal import Decimal

import pytest

from app.arithmetic_lines import checked_lines, credit_signed, names_another_document, read_lines
from app.layout_extract import _normalise_words, extract_invoice

WIDTH, HEIGHT = 600.0, 800.0
# Where a row's cells start, across the page: code, text, then figures that end at these right edges.
RIGHT_EDGES = {"a": 330, "b": 400, "c": 470, "d": 530, "e": 590}


def page(*rows, page_no=1, start=100.0):
    """Word boxes for rows of cells: text left-aligned from x=30, a (column, text) cell right-aligned on its edge."""
    boxes = []
    for n, cells in enumerate(rows):
        y = start + n * 18
        left = 30.0
        for cell in cells:
            if isinstance(cell, tuple):
                edge, text = RIGHT_EDGES[cell[0]], cell[1]
                x0 = edge - 6 * len(text)
                boxes.append(_box(text, x0, y, edge, page_no))
            else:
                for word in cell.split():
                    boxes.append(_box(word, left, y, left + 6 * len(word), page_no))
                    left += 6 * len(word) + 6
    return boxes


def _box(text, x0, y, x1, page_no):
    return {"text": text, "box": [x0, y, x1, y + 10], "page": page_no, "size": [WIDTH, HEIGHT]}


def read(*rows):
    return read_lines(_normalise_words(page(*rows)))


def row(text, qty, price, amount, *more):
    return [text, ("a", qty), ("b", price), ("c", amount), *more]


def test_rows_are_read_from_their_arithmetic_whatever_the_table_calls_its_columns():
    lines = read(
        ["Ref", "Goods", ("a", "Lifted"), ("b", "Each"), ("c", "Sum")],
        row("BR-104 Brass hinge 40mm PAIR", "12", "4.50", "54.00"),
        row("TL-221 Tile adhesive", "3", "19.25", "57.75"),
        ["Amount payable", ("c", "111.75")],
    )
    assert [(l.sku, l.uom, l.qty, l.price, l.net_amount) for l in lines] == [
        ("BR-104", None, 12, Decimal("4.50"), Decimal("54.00")),
        ("TL-221", None, 3, Decimal("19.25"), Decimal("57.75")),
    ]


def test_a_barcode_is_kept_as_the_barcode_and_never_multiplied():
    lines = read(
        row("BR-104 4006381333931 Brass hinge", "12", "4.50", "54.00"),
        row("TL-221 5901234123457 Tile adhesive", "3", "19.25", "57.75"),
    )
    assert [(l.gtin, l.net_amount) for l in lines] == [
        ("4006381333931", Decimal("54.00")), ("5901234123457", Decimal("57.75"))]


@pytest.mark.parametrize("rows", [
    # Rate x taxable = tax, with the rate as a percent, as a bare fraction, and under a tax heading.
    [row("Standard rate 5%", "1000.00", "0.05", "50.00"), row("Reduced rate 2%", "500.00", "0.02", "10.00")],
    [["Band", ("a", "Rate"), ("b", "Base"), ("c", "VAT")], row("Goods", "5", "10.00", "50.00"), row("Works", "2", "10.00", "20.00")],
    [["VAT", ("a", "5%"), ("b", "1000.00"), ("c", "50.00")], ["VAT", ("a", "0%"), ("b", "200.00"), ("c", "0.00")]],
    # Totals blocks.
    [row("Sub total", "2", "250.00", "500.00"), row("Grand total", "3", "200.00", "600.00")],
    [row("Tax", "2", "25.00", "50.00"), row("Balance due", "2", "275.00", "550.00")],
    # Figures with nothing printed that could be a row.
    [[("a", "2"), ("b", "25.00"), ("c", "50.00")], [("a", "3"), ("b", "10.00"), ("c", "30.00")]],
], ids=["rate-as-fraction", "tax-heading", "percent", "totals", "tax-and-balance", "no-text"])
def test_summaries_and_totals_never_become_lines(rows):
    assert read(*rows) == []


def test_a_tax_summary_and_totals_below_the_table_stay_out_of_it():
    lines = read(
        row("BR-104 Brass hinge", "12", "4.50", "54.00"),
        row("TL-221 Tile adhesive", "3", "19.25", "57.75"),
        ["Sub total", ("c", "111.75")],
        row("Goods at standard", "2", "55.875", "111.75"),
        ["VAT 5%", ("c", "5.59")],
    )
    assert [l.sku for l in lines] == ["BR-104", "TL-221"]


def test_one_times_itself_proves_nothing_alone():
    assert read(row("Exchange rate", "1", "3.67", "3.67")) == []
    assert read(["Page", ("a", "1"), ("b", "1"), ("c", "1")]) == []


def test_quantity_one_with_price_equal_to_amount_is_a_row_when_the_table_or_the_total_confirms_it():
    in_table = read(
        row("BR-104 Brass hinge", "12", "4.50", "54.00"),
        row("TL-221 Site visit", "1", "75.00", "75.00"),
        row("TL-400 Tile adhesive", "3", "19.25", "57.75"),
    )
    assert [(l.qty, l.net_amount) for l in in_table] == [
        (12, Decimal("54.00")), (1, Decimal("75.00")), (3, Decimal("57.75"))]
    alone = read(row("Site visit and report", "1", "75.00", "75.00"), ["Amount payable", ("c", "75.00")])
    assert [(l.qty, l.price, l.net_amount) for l in alone] == [(1, Decimal("75.00"), Decimal("75.00"))]


def test_a_rounded_amount_multiplies_and_a_wrong_one_is_held_with_a_flag():
    lines = read(
        row("AA-1 Rounded", "3", "33.33", "100.00"),
        row("AA-2 Misread", "3", "33.33", "108.00"),
        row("AA-3 Exact", "2", "10.50", "21.00"),
    )
    assert [(l.qty, l.price, l.net_amount) for l in lines] == [
        (3, Decimal("33.33"), Decimal("100.00")),
        (None, None, None),  # Held: the line rules flag a quantity or unit cost missing.
        (2, Decimal("10.50"), Decimal("21.00")),
    ]
    assert lines[1].sku == "AA-2" and "108.00" in lines[1].evidence


def test_a_discount_between_price_and_amount_is_read_and_the_amount_is_the_one_after_it():
    percent = read(
        row("AA-1 One", "10", "5.00", "45.00", ("d", "10%")),
        row("AA-2 Two", "4", "25.00", "90.00", ("d", "10%")),
    )
    assert [(l.qty, l.price, l.net_amount) for l in percent] == [
        (10, Decimal("5.00"), Decimal("45.00")), (4, Decimal("25.00"), Decimal("90.00"))]
    after = read(
        row("AA-1 One", "10", "5.00", "50.00", ("d", "2.50"), ("e", "47.50")),
        row("AA-2 Two", "4", "25.00", "100.00", ("d", "5.00"), ("e", "95.00")),
    )
    assert [(l.price, l.net_amount) for l in after] == [(Decimal("5.00"), Decimal("47.50")), (Decimal("25.00"), Decimal("95.00"))]


def test_a_tax_column_beside_the_amount_does_not_replace_it():
    lines = read(
        row("AA-1 One", "10", "5.00", "50.00", ("d", "5%"), ("e", "2.50")),
        row("AA-2 Two", "4", "25.00", "100.00", ("d", "5%"), ("e", "5.00")),
    )
    assert [(l.net_amount, l.tax_amount) for l in lines] == [
        (Decimal("50.00"), Decimal("2.50")), (Decimal("100.00"), Decimal("5.00"))]


def test_a_tax_inclusive_amount_does_not_multiply_so_no_table_is_invented_from_it():
    assert read(
        row("AA-1 One", "2", "100.00", "210.00", ("d", "5%")),
        row("AA-2 Two", "4", "25.00", "105.00", ("d", "5%")),
    ) == []


def test_a_tariff_code_that_happens_to_multiply_does_not_move_the_columns():
    lines = read(
        ["AA-1 Goods tariff", ("a", "12"), ("b", "4.50"), ("c", "54.00"), ("d", "8"), ("e", "432")],
        row("AA-2 More goods", "3", "19.25", "57.75", ("d", "7")),
        row("AA-3 Other goods", "5", "2.00", "10.00", ("d", "6")),
    )
    assert [l.net_amount for l in lines] == [Decimal("54.00"), Decimal("57.75"), Decimal("10.00")]
    assert read(["Tariff heading", ("a", "12"), ("b", "4"), ("c", "48")]) == []


def test_quantity_and_price_that_cannot_be_told_apart_are_held_with_the_amount():
    lines = read(row("AA-1 One", "12", "40", "480"), row("AA-2 Two", "25", "30", "750"))
    assert [(l.qty, l.price, l.net_amount) for l in lines] == [(None, None, Decimal("480")), (None, None, Decimal("750"))]


def test_a_heading_it_knows_can_tell_quantity_from_price():
    lines = read(
        ["Goods", ("a", "Unit Price"), ("b", "Items")],
        row("AA-1 One", "40", "12", "480"),
        row("AA-2 Two", "30", "25", "750"),
    )
    assert [(l.qty, l.price) for l in lines] == [(12, 40), (25, 30)]


def test_a_count_with_decimals_beside_a_whole_money_column_is_still_the_count():
    lines = read(row("AA-1 One", "2.5", "1200", "3000"), row("AA-2 Two", "14", "900", "12600"))
    assert [(l.qty, l.price) for l in lines] == [(Decimal("2.5"), 1200), (14, 900)]


def test_a_scan_that_lost_its_decimal_marks_keeps_the_quantity_and_holds_the_money():
    lines = read(row("AA-1 One", "200", "16.156", "3,231.200"), row("AA-2 Two", "69", "5,359", "369.771"))
    assert [(l.qty, l.price, l.net_amount) for l in lines] == [(200, None, None), (69, None, None)]


@pytest.mark.parametrize("printed, qty, price, amount", [
    (("3", "1.250,50", "3.751,50"), 3, "1250.50", "3751.50"),
    (("3", "1'250.50", "3'751.50"), 3, "1250.50", "3751.50"),
    (("3", "1,250.50", "3,751.50"), 3, "1250.50", "3751.50"),
    (("20", "1,10,000.50", "22,00,010.00"), 20, "110000.50", "2200010.00"),
    (("3", "٤٢٣٫٧٩١", "١٬٢٧١٫٣٧٣"), 3, "423.791", "1271.373"),
])
def test_the_arithmetic_reads_every_way_of_printing_a_number(printed, qty, price, amount):
    lines = read(row("AA-1 One", *printed), row("AA-2 Two", *printed))
    assert (lines[0].qty, lines[0].price, lines[0].net_amount) == (qty, Decimal(price), Decimal(amount))


def test_a_figure_grouped_by_a_space_is_read():
    spaced = read(
        ["AA-1 One PKT 263 281,57 74 052,91"],
        ["AA-2 Two PKT 72 1 922,54 138 422,88"],
    )
    assert [(l.qty, l.price, l.net_amount) for l in spaced] == [
        (263, Decimal("281.57"), Decimal("74052.91")), (72, Decimal("1922.54"), Decimal("138422.88"))]


def test_a_credit_marked_amount_is_negative():
    lines = read(
        row("AA-1 One", "2", "10.00", "20.00", ("d", "CR")),
        row("AA-2 Two", "3", "10.00", "30.00", ("d", "CR")),
    )
    assert [l.net_amount for l in lines] == [Decimal("-20.00"), Decimal("-30.00")]


def test_a_reader_that_cuts_a_printed_number_into_pieces_is_put_back_together():
    words = []
    for n, (line, cut) in enumerate((
        ("AA-1 One 3 2,423.34 7,270.02", ["AA-1", "One", "3", "2", ",", "423.34", "7", ",", "270.02"]),
        ("AA-2 Two 5 1,000.50 5,002.50", ["AA-2", "Two", "5", "1", ",", "000.50", "5", ",", "002.50"]),
    )):
        x = 30.0
        for piece in cut:
            words.append({"text": piece, "box": [x, 100 + n * 18, x + 6 * len(piece), 110 + n * 18], "page": 1,
                          "size": [WIDTH, HEIGHT], "parent_text": line})
            x += 6 * len(piece) + 2
    lines = read_lines(_normalise_words(words))
    assert [(l.sku, l.qty, l.price, l.net_amount) for l in lines] == [
        ("AA-1", 3, Decimal("2423.34"), Decimal("7270.02")), ("AA-2", 5, Decimal("1000.50"), Decimal("5002.50"))]


def invoice_boxes(*title):
    return page(
        [*title],
        ["Number", "HX-77"],
        row("BR-104 Brass hinge", "12", "4.50", "54.00"),
        row("TL-221 Tile adhesive", "3", "19.25", "57.75"),
        ["Amount payable", ("c", "111.75")],
    )


def test_a_document_with_a_confirmed_priced_table_is_read_whatever_its_title_calls_it():
    boxes = invoice_boxes("Remittance", "demand")
    invoice = extract_invoice("Remittance demand", boxes)
    assert invoice is not None and len(invoice["lines"]) == 2


@pytest.mark.parametrize("title", ["Quotation", "Proforma Invoice", "Delivery Note", "Statement of Account",
                                   "Purchase Order", "Packing List"])
def test_a_document_that_names_itself_something_else_gets_no_lines_from_the_arithmetic(title):
    assert names_another_document(f"{title}\nNumber HX-77")
    invoice = extract_invoice(f"{title}\nNumber HX-77", invoice_boxes(*title.split()))
    assert invoice is None or not invoice["lines"]


@pytest.mark.parametrize("label", ["Purchase Order: 4471", "Delivery Note 88", "Statement of Account 12 for May"])
def test_a_reference_an_invoice_prints_is_not_a_title(label):
    assert not names_another_document(f"Remittance\n{label}")


def test_no_title_and_no_confirmed_table_still_reads_nothing():
    assert extract_invoice("Remittance\nThanks", page(["Remittance"], ["Thanks"])) is None


def _heading_row(**cells):
    return {"sku": None, "qty": None, "price": None, "net_amount": None, "tax_amount": None, **cells}


TWO_ROWS = (row("BR-104 Hinge", "12", "4.50", "54.00"), row("TL-221 Adhesive", "3", "19.25", "57.75"))


def test_a_price_that_is_really_the_amount_takes_the_figures_the_rows_multiply_to():
    # A heading took the amount column for the price, and left no amount to check it against.
    read_by_heading = [_heading_row(sku="BR-104", qty="12", price="54.00"), _heading_row(sku="TL-221", qty="3", price="57.75")]
    lines = checked_lines(read_by_heading, _normalise_words(page(*TWO_ROWS)))
    assert [(l["sku"], Decimal(str(l["price"])), Decimal(str(l["net_amount"]))) for l in lines] == [
        ("BR-104", Decimal("4.50"), Decimal("54.00")), ("TL-221", Decimal("19.25"), Decimal("57.75"))]


def test_a_price_nothing_can_confirm_is_cleared_so_the_line_is_flagged():
    read_by_heading = [_heading_row(sku="BR-104", qty="12", price="54.00"), _heading_row(sku="TL-221", qty="3", price="57.75")]
    rows = (["BR-104 Hinge", ("a", "12"), ("b", "54.00")], ["TL-221 Adhesive", ("a", "3"), ("b", "57.75")])
    assert checked_lines(read_by_heading, _normalise_words(page(*rows))) == read_by_heading
    same_count = (row("BR-104 Hinge", "12", "4.50", "54.00"), row("TL-221 Adhesive", "3", "19.25", "60.00"))
    lines = checked_lines(read_by_heading, _normalise_words(page(*same_count, row("ZZ-1 Spare", "2", "2.00", "4.00"))))
    assert len(lines) == 3


def test_rows_a_heading_read_that_multiply_are_left_exactly_as_read():
    read_by_heading = [_heading_row(sku="X-1", qty="2", price="10.00", net_amount="20.00", description="As the heading read it")]
    assert checked_lines(read_by_heading, _normalise_words(page(*TWO_ROWS))) is read_by_heading


@pytest.mark.parametrize("title", ["Credit Note", "CREDIT MEMO", "Sales Return Note", "Refund Advice"])
def test_a_document_titled_a_credit_has_negative_amounts_whatever_it_prints(title):
    invoice = credit_signed({"net": "50.00", "tax": None, "lines": [_heading_row(net_amount="50.00", qty="2", price="25.00")]},
                            f"Acme\n{title}\nNumber 4")
    assert invoice["net"] == Decimal("-50.00") and invoice["tax"] is None
    assert invoice["lines"][0]["net_amount"] == Decimal("-50.00")
    assert invoice["lines"][0]["qty"] == "2" and invoice["lines"][0]["price"] == "25.00"


@pytest.mark.parametrize("text", ["Tax Invoice\nCredit Note No: 44", "Invoice\nCredit terms 30 days", "Invoice\nReturn policy note"])
def test_a_mention_of_a_credit_changes_no_sign(text):
    assert credit_signed({"net": "50.00", "lines": []}, text)["net"] == "50.00"


def test_a_page_turned_over_is_put_the_right_way_up():
    from app.page_turn import turn_pages
    upright = page(
        ["Remittance"],
        row("BR-104 Brass hinge", "12", "4.50", "54.00"),
        row("TL-221 Tile adhesive", "3", "19.25", "57.75"),
        ["Amount payable", ("c", "111.75")],
    )
    assert turn_pages(upright) is None
    over = [{**word, "box": [WIDTH - word["box"][2], HEIGHT - word["box"][3], WIDTH - word["box"][0], HEIGHT - word["box"][1]]}
            for word in reversed(upright)]
    turned = turn_pages(over)
    assert turned is not None
    assert [(l.sku, l.qty, l.net_amount) for l in read_lines(_normalise_words(turned))] == [
        ("BR-104", 12, Decimal("54.00")), ("TL-221", 3, Decimal("57.75"))]


@pytest.mark.parametrize("inverse", [1, 3])
def test_a_page_on_its_side_turns_the_way_its_totals_say(inverse):
    from app.page_turn import _turned, turn_pages
    upright = page(
        ["Remittance", "note", "for", "goods", "lifted"],
        row("BR-104 Brass hinge", "12", "4.50", "54.00"),
        row("TL-221 Tile adhesive", "3", "19.25", "57.75"),
        ["Grand total", ("c", "111.75")],
    )
    sideways = [{**_turned(word, inverse), "geometry": "word"} for word in upright]
    turned = turn_pages(sideways)
    assert turned is not None
    assert [(l.sku, l.net_amount) for l in read_lines(_normalise_words(turned))] == [
        ("BR-104", Decimal("54.00")), ("TL-221", Decimal("57.75"))]


def test_a_sideways_page_with_no_way_to_tell_stays_as_it_is():
    from app.page_turn import _turned, turn_pages
    sideways = [_turned(word, 1) for word in page(["Goods", "note", "printed", "without", "figures", "anywhere", "here", "either", "ever", "again"])]
    assert turn_pages(sideways) is None


def test_figures_first_on_an_upright_page_do_not_turn_it():
    from app.page_turn import turn_pages
    amounts_first = page(
        ["Remittance"],
        [("a", "54.00"), ("b", "12"), ("c", "4.50"), "BR-104 hinge"],
        [("a", "57.75"), ("b", "3"), ("c", "19.25"), "TL-221 adhesive"],
        [("a", "111.75"), "Amount payable"],
    )
    assert turn_pages(amounts_first) is None


@pytest.mark.parametrize("sku, description", [
    ("Hinge", "brass 40mm"),  # A word in the code column; the code lost.
    ("4006381333931", "Hinge brass 40mm"),  # The barcode as the code.
    (None, "BR-104 Hinge brass 40mm"),  # The code inside the description.
])
def test_a_code_a_heading_misplaced_comes_from_the_row_itself(sku, description):
    read_by_heading = [_heading_row(sku=sku, description=description, qty="12", price="4.50", net_amount="54.00"),
                       _heading_row(sku="TL-221", description="Adhesive", qty="3", price="19.25", net_amount="57.75")]
    rows = (row("BR-104 Hinge brass 40mm", "12", "4.50", "54.00"), row("TL-221 Adhesive", "3", "19.25", "57.75"))
    lines = checked_lines(read_by_heading, _normalise_words(page(*rows)))
    assert lines[0]["sku"] == "BR-104" and lines[0]["description"] == "Hinge brass 40mm"
    assert lines[1] == read_by_heading[1]


def test_a_word_for_a_code_stays_when_the_row_has_no_code_either():
    read_by_heading = [_heading_row(sku="HINGE", description="Brass", qty="12", price="4.50", net_amount="54.00"),
                       _heading_row(sku="GLUE", description="Tile", qty="3", price="19.25", net_amount="57.75")]
    rows = (row("HINGE Brass", "12", "4.50", "54.00"), row("GLUE Tile", "3", "19.25", "57.75"))
    assert checked_lines(read_by_heading, _normalise_words(page(*rows))) == read_by_heading


def test_a_different_row_gives_no_text():
    read_by_heading = [_heading_row(sku="Hinge", description="brass", qty="12", price="4.50", net_amount="54.00"),
                       _heading_row(sku="X-1", description="Tile", qty="3", price="19.25", net_amount="57.75")]
    rows = (row("BR-104 Hinge", "2", "5.00", "10.00"), row("X-1 Tile", "3", "19.25", "57.75"))
    assert checked_lines(read_by_heading, _normalise_words(page(*rows)))[0]["sku"] == "Hinge"


def test_a_narrow_receipt_prints_its_text_on_the_row_above_the_figures():
    lines = read(
        ["BR-104 4006381333931 Hinge brass 40mm"],
        ["12 PAIR x", ("b", "4.50"), ("c", "54.00")],
        ["TL-221 5901234123457 Tile adhesive"],
        ["3 BAG x", ("b", "19.25"), ("c", "57.75")],
    )
    assert [(l.sku, l.gtin, l.description, l.uom, l.qty) for l in lines] == [
        ("BR-104", "4006381333931", "Hinge brass 40mm", "PAIR", 12),
        ("TL-221", "5901234123457", "Tile adhesive", "BAG", 3)]


def test_a_row_with_three_decimals_is_read_by_the_mark_another_row_settles():
    lines = read(
        ["Goods", ("a", "Qty"), ("b", "Each"), ("c", "Sum")],
        row("Thermal roll", "12", "34.768", "417.216"),
        row("Bottle brush", "19", "7.500", "142.500"),
        row("Copper elbow", "443", "48.454", "21,465.122"),
    )

    assert [(l.qty, l.price, l.net_amount) for l in lines] == [
        (12, Decimal("34.768"), Decimal("417.216")),
        (19, Decimal("7.5"), Decimal("142.5")),
        (443, Decimal("48.454"), Decimal("21465.122")),
    ]


def test_three_decimals_nothing_settles_stay_held():
    lines = read(row("Thermal roll", "12", "34.768", "417.216"), row("Bottle brush", "19", "7.500", "142.500"))

    assert all(line.net_amount is None for line in lines)


def test_a_long_item_number_that_is_no_barcode_keeps_a_heading_read_as_it_is():
    words = _normalise_words(page(
        ["Item", "Goods", ("a", "Qty"), ("b", "Each"), ("d", "Disc"), ("c", "Sum")],
        ["009900000001", "Hinge", ("a", "4"), ("b", "2.500"), ("d", "0.000"), ("c", "10.000")],
        ["009900000002", "Hasp", ("a", "3"), ("b", "2.000"), ("d", "0.000"), ("c", "6.000")],
    ))
    lines = [
        {"sku": "009900000001", "description": "Hinge", "uom": None, "qty": "4", "price": "2.500", "net_amount": "10.000"},
        {"sku": "009900000002", "description": "Hasp", "uom": None, "qty": "3", "price": "2.000", "net_amount": "6.000"},
    ]

    assert checked_lines(lines, words) == lines


def test_text_under_a_row_continues_it_only_where_the_table_is_seen_to_wrap():
    heading = ["Goods", ("a", "Qty"), ("b", "Each"), ("c", "Sum")]
    wraps = read(
        heading,
        row("Basmati rice 5kg bag", "4", "2.50", "10.00"),
        ["with protective wrap"],
        row("Coconut water 330ml", "3", "2.00", "6.00"),
        ["in a carton"],
    )
    footer = read(heading, row("Basmati rice", "4", "2.50", "10.00"), row("Coconut water", "3", "2.00", "6.00"), ["Goods once sold"])

    assert [line.description for line in wraps] == ["Basmati rice 5kg bag with protective wrap", "Coconut water 330ml in a carton"]
    assert [line.description for line in footer] == ["Basmati rice", "Coconut water"]


def test_arabic_held_as_glyphs_on_the_page_is_put_back_as_the_words_it_spells():
    lines = read(["ﻭﺭﻕ", "ﻃﺒﺎﻋﺔ", ("a", "4"), ("b", "2.50"), ("c", "10.00")], ["ﻣﺎﺀ", ("a", "3"), ("b", "2.00"), ("c", "6.00")])

    assert lines[0].description == "طباعة ورق"


def test_a_turned_page_is_still_seen_when_a_scanned_row_lost_its_arithmetic():
    from app.page_turn import turn_pages
    upright = page(
        ["Remittance"],
        ["PV-1 Copper tape", ("a", "[6"), ("b", "69.72"), ("c", "6,344.52")],
        row("BR-104 Brass hinge", "12", "4.50", "54.00"),
        row("TL-221 Tile adhesive", "3", "19.25", "57.75"),
        ["Amount payable", ("c", "6,456.27")],
    )
    over = [{**word, "box": [WIDTH - word["box"][2], HEIGHT - word["box"][3], WIDTH - word["box"][0], HEIGHT - word["box"][1]]}
            for word in reversed(upright)]

    assert turn_pages(over) is not None


def test_a_page_on_its_side_turns_on_a_total_with_the_currency_touching_it():
    from app.page_turn import _turned, turn_pages
    upright = page(
        ["Remittance", "note", "for", "goods", "lifted"],
        row("BR-104 Brass hinge", "12", "4.50", "54.00"),
        row("TL-221 Tile adhesive", "3", "19.25", "57.75"),
        ["Grand total", ("c", "EGP111.75")],
    )
    sideways = [{**_turned(word, 1), "geometry": "word"} for word in upright]

    assert read_lines(_normalise_words(turn_pages(sideways)))[0].sku == "BR-104"
