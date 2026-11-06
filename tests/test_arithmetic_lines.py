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


@pytest.mark.parametrize("label", ["Purchase Order: 4471", "Our Delivery Note 88", "See Statement of Account 12 for May"])
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


def _lost_marks_page():
    return page(
        ["Goods", ("a", "Qty"), ("b", "Each"), ("c", "Sum")],
        row("BR-104 Brass hinge", "197", "225.021", "44,329.137"),
        row("TL-221 Tile adhesive", "60", "1.044.421", "62.665.260"),
        row("CP-310 Copper elbow", "32", "251.692", "8.054.144"),
    )


def test_a_scan_that_turned_group_marks_into_points_reads_at_the_scale_the_table_proves():
    assert [line.net_amount for line in read_lines(_normalise_words(_lost_marks_page()))] == [
        Decimal("44329.137"), Decimal("62665.260"), Decimal("8054.144")]


def test_a_heading_read_a_thousand_times_too_large_takes_the_scale_the_table_proves():
    lines = [
        {"sku": "BR-104", "description": "Brass hinge", "qty": "197", "price": "225.021", "net_amount": "44329.137"},
        {"sku": "TL-221", "description": "Tile adhesive", "qty": "60", "price": "1044421", "net_amount": "62665260"},
        {"sku": "CP-310", "description": "Copper elbow", "qty": "32", "price": "251692", "net_amount": "8054144"},
    ]

    checked = checked_lines(lines, _normalise_words(_lost_marks_page()))

    assert [(Decimal(str(l["price"])), Decimal(str(l["net_amount"]))) for l in checked] == [
        (Decimal("225.021"), Decimal("44329.137")),
        (Decimal("1044.421"), Decimal("62665.260")),
        (Decimal("251.692"), Decimal("8054.144")),
    ]


def test_whole_figures_beside_three_decimals_that_no_read_can_settle_are_blanked():
    lines = [
        {"sku": "BR-104", "description": "Brass hinge", "qty": "197", "price": "225.021", "net_amount": "44329.137"},
        {"sku": "TL-221", "description": "Tile adhesive", "qty": "60", "price": "1044421", "net_amount": "62665260"},
    ]

    checked = checked_lines(lines, _normalise_words(page(["Nothing", "multiplies", "here"])))

    assert checked[0]["net_amount"] == "44329.137"
    assert checked[1]["price"] is None and checked[1]["net_amount"] is None


def test_whole_figures_a_page_really_prints_stay():
    lines = [
        {"sku": "BR-104", "description": "Brass hinge", "qty": "4", "price": "2.125", "net_amount": "8.500"},
        {"sku": "TL-221", "description": "Tile adhesive", "qty": "3", "price": "100", "net_amount": "300"},
    ]
    words = _normalise_words(page(
        ["Goods", ("a", "Qty"), ("b", "Each"), ("c", "Sum")],
        row("BR-104 Brass hinge", "4", "2.125", "8.500"),
        row("TL-221 Tile adhesive", "3", "100", "300"),
    ))

    assert [line["net_amount"] for line in checked_lines(lines, words)] == ["8.500", "300"]


def test_group_marks_a_scan_read_as_points_before_two_decimals_are_one_number():
    lines = read(
        ["Goods", ("a", "Qty"), ("b", "Each"), ("c", "Sum")],
        row("BR-104 Brass hinge", "359", "1.422.46", "510.663.14"),
        row("TL-221 Tile adhesive", "144", "373.22", "53.743.68"),
    )

    assert [line.net_amount for line in lines] == [Decimal("510663.14"), Decimal("53743.68")]


def _table(*cells_per_row, heading=("Ref", "Goods")):
    """Rows whose text cells start at fixed places: (x, word) pairs, then qty, price and amount."""
    boxes = []
    for x, word in zip((30, 110), heading):
        boxes.append(_box(word, x, 100, x + 6 * len(word), 1))
    for edge, word in zip((330, 400, 470), ("Lifted", "Each", "Sum")):
        boxes.append(_box(word, edge - 6 * len(word), 100, edge, 1))
    for n, (cells, figures) in enumerate(cells_per_row, start=1):
        y = 100 + n * 18
        for x, text in cells:
            for word in text.split():
                boxes.append(_box(word, x, y, x + 6 * len(word), 1))
                x += 6 * len(word) + 6
        for edge, figure in zip((330, 400, 470), figures):
            boxes.append(_box(figure, edge - 6 * len(figure), y, edge, 1))
    return read_lines(_normalise_words(boxes))


def test_batch_and_expiry_columns_beside_the_row_are_not_its_description():
    lines = _table(
        (((30, "BR-104"), (90, "Brass hinge 40mm"), (210, "L7741"), (250, "03/2028")), ("12", "4.50", "54.00")),
        (((30, "TL-221"), (90, "Tile adhesive"), (210, "L7802"), (250, "11/2027")), ("3", "19.25", "57.75")),
        (((30, "PX-330"), (90, "Pipe clip small"), (210, "L7911"), (250, "01/2029")), ("10", "1.25", "12.50")),
    )

    assert [line.description for line in lines] == ["Brass hinge 40mm", "Tile adhesive", "Pipe clip small"]


def test_a_code_printed_to_the_right_of_the_description_is_still_the_code():
    lines = _table(
        (((30, "Brass hinge 40mm"), (170, "BR-104")), ("12", "4.50", "54.00")),
        (((30, "Tile adhesive"), (170, "TL-221")), ("3", "19.25", "57.75")),
    )

    assert [(line.sku, line.description) for line in lines] == [("BR-104", "Brass hinge 40mm"), ("TL-221", "Tile adhesive")]


def test_a_barcode_column_is_not_taken_for_the_code():
    lines = _table(
        (((30, "4006381333931"), (120, "BR-104"), (180, "Brass hinge")), ("12", "4.50", "54.00")),
        (((30, "4006381333948"), (120, "TL-221"), (180, "Tile adhesive")), ("3", "19.25", "57.75")),
    )

    assert [line.sku for line in lines] == ["BR-104", "TL-221"]
    assert [line.description for line in lines] == ["Brass hinge", "Tile adhesive"]


@pytest.mark.parametrize("units", [("DOZ", "PKT"), ("BOX", "BOX")])
def test_a_unit_column_stays_when_most_of_its_cells_are_units(units):
    lines = _table(
        (((30, "BR-104"), (90, "Brass hinge"), (210, units[0])), ("12", "4.50", "54.00")),
        (((30, "TL-221"), (90, "Tile adhesive"), (210, units[1])), ("3", "19.25", "57.75")),
    )

    assert [line.uom for line in lines] == list(units)
    assert [line.description for line in lines] == ["Brass hinge", "Tile adhesive"]


def test_codes_with_a_slash_are_codes_not_dates():
    lines = _table(
        (((30, "5361/6"), (110, "Brass hinge 40mm")), ("12", "4.50", "54.00")),
        (((30, "67350/3"), (110, "Tile adhesive 2kg")), ("3", "19.25", "57.75")),
        (((30, "588108"), (110, "Pipe clip small")), ("10", "1.25", "12.50")),
    )

    assert [line.sku for line in lines] == ["5361/6", "67350/3", "588108"]
    assert lines[0].description == "Brass hinge 40mm"


def test_a_side_panel_beside_the_table_is_not_part_of_the_rows():
    lines = _table(
        (((30, "Brass hinge 40mm"), (230, "Thank you for")), ("12", "4.50", "54.00")),
        (((30, "Tile adhesive"), (230, "your business")), ("3", "19.25", "57.75")),
    )

    assert [line.description for line in lines] == ["Brass hinge 40mm", "Tile adhesive"]


def test_the_tax_is_not_taken_for_the_amount_when_net_tax_and_gross_are_all_printed():
    lines = read(
        ["Goods", ("a", "Lifted"), ("b", "Each"), ("c", "Sum")],
        row("BR-104 Brass hinge", "328", "42.03", "13,785.84", ("d", "1,378.58"), ("e", "15,164.42")),
        row("TL-221 Tile adhesive", "235", "11.12", "2,613.20", ("d", "261.32"), ("e", "2,874.52")),
    )

    assert [line.net_amount for line in lines] == [Decimal("13785.84"), Decimal("2613.20")]
    assert [line.tax_amount for line in lines] == [Decimal("1378.58"), Decimal("261.32")]


def test_a_credit_slip_takes_money_back_like_any_credit_title():
    invoice = {"net": "10.00", "lines": [{"net_amount": "10.00", "tax_amount": None}]}

    signed = credit_signed(invoice, "Saltworks Ltd\nCredit Slip\nRef 4471")

    assert signed["net"] == Decimal("-10.00") and signed["lines"][0]["net_amount"] == Decimal("-10.00")


def test_a_row_whose_discount_rate_did_not_read_still_settles_in_the_tables_column():
    lines = read(
        ["Goods", ("a", "Lifted"), ("b", "Each"), ("c", "Gross"), ("d", "Off"), ("e", "Net")],
        row("BR-104 Brass hinge", "10", "5.00", "50.00", ("d", "10%"), ("e", "45.00")),
        row("TL-221 Tile adhesive", "4", "25.00", "100.00", ("e", "95.00")),
        row("PX-330 Pipe clip", "20", "10.00", "200.00", ("d", "5%"), ("e", "190.00")),
    )

    assert [line.net_amount for line in lines] == [Decimal("45.00"), Decimal("95.00"), Decimal("190.00")]


def test_bars_of_a_table_ruled_in_text_are_not_part_of_its_cells():
    lines = read(
        ["|Item", "No", "|Goods", "|Lifted|", "Each|", ("c", "Sum|")],
        ["|BR-104", "|Brass", "hinge", ("a", "12|"), ("b", "4.50|"), ("c", "54.00|")],
        ["|TL-221", "|Tile", "adhesive", ("a", "3|"), ("b", "19.25|"), ("c", "57.75|")],
    )

    assert [line.net_amount for line in lines] == [Decimal("54.00"), Decimal("57.75")]
    assert [line.sku for line in lines] == ["BR-104", "TL-221"]


def test_a_price_headed_per_hundred_multiplies_at_its_scale():
    lines = read(
        ["Goods", ("a", "Lifted"), ("b", "per 100"), ("c", "Sum")],
        row("BR-104 Brass hinge", "12", "450.00", "54.00"),
        row("TL-221 Tile adhesive", "3", "1,925.00", "57.75"),
    )

    assert [line.net_amount for line in lines] == [Decimal("54.00"), Decimal("57.75")]


def test_a_rate_times_a_base_with_no_per_hundred_heading_is_no_line():
    assert read(
        ["Rate", ("a", "Base"), ("c", "Charge")],
        ["Band one", ("a", "5"), ("b", "1,000.00"), ("c", "50.00")],
        ["Band two", ("a", "10"), ("b", "2,000.00"), ("c", "200.00")],
    ) == []


def _sections(last_row):
    return (
        ["Goods", ("a", "Lifted"), ("b", "Each"), ("c", "Sum")],
        ["Section one"],
        row("BR-104 Brass hinge", "12", "4.50", "54.00"),
        row("TL-221 Tile adhesive", "3", "19.25", "57.75"),
        ["Section total", ("c", "111.75")],
        ["Section two"],
        row("PX-330 Pipe clip", "10", "1.25", "12.50"),
        row("GS-512 Glass sheet", "2", "40.00", "80.00"),
        ["Section total", ("c", "92.50")],
        ["Goods total", ("c", last_row)],
    )


def test_a_section_total_does_not_end_a_table_when_all_rows_add_up_below():
    assert len(read(*_sections("204.25"))) == 4


def test_rows_after_a_total_are_not_lines_unless_all_of_them_add_up():
    assert len(read(*_sections("999.99"))) == 2


def test_a_tax_block_after_a_subtotal_never_joins_the_table():
    lines = read(
        ["Goods", ("a", "Lifted"), ("b", "Each"), ("c", "Sum")],
        row("BR-104 Brass hinge", "12", "4.50", "54.00"),
        row("TL-221 Tile adhesive", "3", "19.25", "57.75"),
        ["Subtotal", ("c", "111.75")],
        ["Standard band", ("a", "5"), ("b", "1.00"), ("c", "5.00")],
        ["Lower band", ("a", "10"), ("b", "2.00"), ("c", "20.00")],
        ["Total tax", ("c", "25.00")],
        ["To pay", ("c", "136.75")],
    )

    assert len(lines) == 2


def test_fees_with_no_price_are_lines_only_when_they_add_up_to_the_total_below():
    rows = [
        ["Particulars of work", ("c", "Fee")],
        ["Pump maintenance visit", ("c", "500.25")],
        ["Forklift hire week", ("c", "300.50")],
        ["Cold room repair", ("c", "199.25")],
    ]

    lines = read(*rows, ["Sum", ("c", "1,000.00")])
    assert [line.net_amount for line in lines] == [Decimal("500.25"), Decimal("300.50"), Decimal("199.25")]
    assert [line.qty for line in lines] == [None, None, None] and [line.price for line in lines] == [None, None, None]
    assert lines[0].description == "Pump maintenance visit"
    assert read(*rows, ["Sum", ("c", "1,000.01")]) == []


def test_figures_with_no_heading_over_them_are_not_fees():
    assert read(
        ["Pump maintenance visit", ("c", "500.25")],
        ["Forklift hire week", ("c", "300.50")],
        ["Sum", ("c", "800.75")],
    ) == []


def test_a_tax_column_adding_up_is_not_lines():
    assert read(
        ["Band of goods", ("c", "VAT")],
        ["Standard band", ("c", "500.25")],
        ["Lower band", ("c", "300.50")],
        ["Sum", ("c", "800.75")],
    ) == []


def test_two_panels_of_one_table_read_as_one():
    def cells(y, left, right):
        return [_box(text, x, y, x + 6 * len(text), 1) for x, text in zip((30, 100, 200, 260), left)] + [
            _box(text, x, y, x + 6 * len(text), 1) for x, text in zip((320, 390, 490, 550), right)]
    boxes = cells(100, ("Code", "Goods", "Count", "Ext."), ("Code", "Goods", "Count", "Ext."))
    boxes += cells(118, ("BR-104", "Hinge", "12", "54.00"), ("PX-330", "Clip", "10", "12.50"))
    boxes += cells(136, ("TL-221", "Tile", "3", "57.75"), ("GS-512", "Glass", "2", "80.00"))
    boxes += [_box("Goods", 300, 170, 330, 1), _box("Total", 336, 170, 366, 1), _box("204.25", 550, 170, 586, 1)]

    lines = read_lines(_normalise_words(boxes))

    assert [line.net_amount for line in lines] == [Decimal(v) for v in ("54.00", "57.75", "12.50", "80.00")]


def test_a_barcode_in_brackets_is_not_the_description():
    lines = read(
        ["Goods", ("a", "Lifted"), ("b", "Each"), ("c", "Sum")],
        row("BR-104 Brass hinge [4006381333931]", "12", "4.50", "54.00"),
        row("TL-221 Tile adhesive[4006381333948]", "3", "19.25", "57.75"),
    )

    assert [line.description for line in lines] == ["Brass hinge", "Tile adhesive"]
    assert [line.gtin for line in lines] == ["4006381333931", "4006381333948"]


def test_a_description_cut_at_a_column_edge_takes_the_rows_own_text():
    words = _normalise_words(page(*TWO_ROWS))
    found = {line.net_amount: line for line in read_lines(words)}
    amount = next(iter(found))
    other = found[amount]
    line = {"sku": other.sku[:3], "description": other.description[:4], "qty": str(other.qty), "price": str(other.price),
            "net_amount": str(amount), "uom": None, "gtin": None}

    checked = checked_lines([line], words)[0]

    assert checked["sku"] == other.sku and checked["description"] == other.description


def test_a_unit_that_is_a_word_of_the_description_was_a_cut_of_it():
    words = _normalise_words(page(*TWO_ROWS))
    other = read_lines(words)[0]
    first, *rest = other.description.split()
    line = {"sku": other.sku.split("-")[0], "description": " ".join(rest), "uom": first, "qty": str(other.qty),
            "price": str(other.price), "net_amount": str(other.net_amount), "gtin": None}

    checked = checked_lines([line], words)[0]

    assert checked["uom"] is None and checked["description"] == other.description


def test_a_code_joined_to_its_barcode_is_split():
    words = _normalise_words(page(*TWO_ROWS))
    other = read_lines(words)[0]
    line = {"sku": None, "description": other.description, "uom": None, "gtin": "KT-51 4006381333931", "qty": str(other.qty),
            "price": str(other.price), "net_amount": str(other.net_amount)}

    checked = checked_lines([line], words)[0]

    assert checked["gtin"] == "4006381333931"


@pytest.mark.parametrize("words", ["credit note.", "issue a credit note.", "Return Slip"])
def test_terms_wrapped_onto_their_own_line_flip_no_sign(words):
    text = f"TAX INVOICE\nBolt 1 10.00 10.00\nNut 1 21.00 21.00\nGoods may be returned against a\n{words}\n"
    invoice = {"net": "31.00", "lines": [{"net_amount": "10.00"}, {"net_amount": "21.00"}]}
    assert credit_signed(invoice, text) is invoice
    assert credit_signed(invoice, f"Terms: on request we\n{words}\nBolt 1 10.00 10.00\nNut 1 21.00 21.00\n")["net"] == (
        Decimal("-31.00") if words == "Return Slip" else "31.00")


def test_a_credit_line_under_a_tax_invoice_title_flips_no_sign():
    invoice = {"net": "31.00", "lines": []}
    assert credit_signed(invoice, "Credit slip\n")["net"] == Decimal("-31.00")
    assert credit_signed(invoice, "TAX INVOICE\nReturn Slip\n") is invoice
    assert credit_signed(invoice, "Tax Invoice SI/2026/1\nCredit Note\n") is invoice
    assert credit_signed(invoice, "Credit Note\nTax Invoice\n")["net"] == Decimal("-31.00")


def test_rows_of_equal_amount_keep_their_own_text():
    same = [{"sku": sku, "description": text, "net_amount": "10.00", "qty": "1", "price": "10.00"}
            for sku, text in (("SKU-1", "Bolt"), ("SKU-2", "Nut"), ("SKU-10", "Bolt M8 zinc"))]
    from app.arithmetic_lines import _uncut
    assert _uncut(same, same) is same
    alone = [{"sku": "SKU-1", "description": "Bolt", "net_amount": "10.00"}]
    assert _uncut(alone, [{"sku": "SKU-1", "description": "Bolt M8 zinc", "net_amount": "10.00"}])[0]["description"] == "Bolt M8 zinc"


@pytest.mark.parametrize("line", ["Quotation No: Q-77", "Delivery Note No: 77", "Statement of Account - January",
                                  "Price List 2026", "Purchase Requisition 77"])
def test_a_numbered_title_of_another_document_says_it_is_not_an_invoice(line):
    assert names_another_document(f"Some Trading LLC\n{line}\nBolt 2 5.00 10.00\n")
    assert not names_another_document("Some Trading LLC\nPurchase Order: 4471\nOur quotation is attached\n")


def test_a_payment_schedule_or_ageing_block_gives_no_lines():
    assert read(["Payment Schedule", ("c", "Sum")], ["Advance on order", ("c", "400.00")], ["On delivery", ("c", "600.00")],
                ["Payable", ("c", "1000.00")]) == []
    assert read(["Stage", ("c", "Sum")], ["First part", ("c", "400.00")], ["Second part", ("c", "600.00")],
                ["Payable", ("c", "1000.00")]) == []
    assert read(["Stage", ("c", "Sum")], ["Advance on order", ("c", "400.00")], ["On delivery", ("c", "600.00")],
                ["Total", ("c", "1000.00")]) == []
    assert read(["Ageing", ("c", "Amount")], ["Current", ("c", "100.00")], ["30 days", ("c", "200.00")],
                ["Total", ("c", "300.00")]) == []


@pytest.mark.parametrize("above", ["Goods Lifted Each Sum", "Please deliver to the rear gate"])
def test_a_row_with_its_own_word_does_not_take_the_row_above(above):
    lines = read([above], row("Bolt", "2", "5.00", "10.00"), row("Nut", "3", "7.00", "21.00"))
    assert [line.description for line in lines] == ["Bolt", "Nut"]


def test_an_invoice_titled_page_printing_a_delivery_note_number_keeps_its_lines():
    rows = (row("BR-104 Hinge", "2", "5.00", "10.00"), row("TL-221 Tile", "3", "19.25", "57.75"), row("X-3 Nut", "1", "4.00", "4.00"))
    boxes = page(["TAX", "INVOICE"], ["Delivery", "Note", "No:", "DN-73"], ["Goods", ("a", "Lifted"), ("b", "Each"), ("c", "Sum")], *rows)
    assert len(extract_invoice("TAX INVOICE\nDelivery Note No: DN-73", boxes)["lines"]) == 3


def test_blank_lines_do_not_slow_the_title_patterns():
    import time
    text = "\n" * 16000
    started = time.perf_counter()
    assert not names_another_document(text) and credit_signed({"net": "1"}, text) == {"net": "1"}
    assert time.perf_counter() - started < 1


def test_out_of_line_rows_with_a_later_zero_do_not_raise():
    from app.arithmetic_lines import _build_rows, _table_end
    shape = _build_rows(_normalise_words(page(row("Bolt", "2", "5.00", "10.00"))))[0].products[0]
    on_page = _build_rows(_normalise_words(page(["Bolt 2 5.00 10.00"], ["Total", ("c", "10.00")], ["Second part"], ["Nut 3 7.00 21.00"], ["Total", ("c", "0.00")])))
    assert _table_end(on_page, [0, 3], shape, []) == 1


def test_a_numbered_delivery_note_under_any_invoice_title_is_still_an_invoice():
    from app.arithmetic_lines import names_another_document

    assert not names_another_document("## VAT Invoice\nDelivery Note No : DN-1\n")
    assert names_another_document("Delivery Note No : DN-1\n")
    assert names_another_document("Proforma Invoice\nDelivery Note No : DN-1\n")
