"""Net and tax a page cannot prove carry a flag, and ones its arithmetic contradicts go blank and flagged.

Every value is made up."""
import pytest

from app.engines import guard_figures


def run(net, tax, text="", lines=None):
    invoice = {"net": net, "tax": tax, "lines": lines or []}
    readers = {"net": "ocr", "tax": "ocr"}
    invoice, evidence = guard_figures(invoice, {"header": {}}, readers, "ocr", text)
    header = evidence["header"]
    return invoice["net"], invoice["tax"], {key: header[key]["review"].get("code") for key in header if header[key].get("review")}


def amounts(*values):
    return [{"net_amount": value} for value in values]


@pytest.mark.parametrize("printed", [
    "Total 1,050.00", "Grand Total: 1050.00", "Amount Due 1,050.00", "Total 1 050,00", "Total 1'050.00", "Total 1.050,00",
    "Total incl VAT 1,050.00",
])
def test_net_plus_tax_is_the_printed_total(printed):
    assert run("1000.00", "50.00", printed) == ("1000.00", "50.00", {})


def test_the_lines_prove_the_net_and_the_rate_the_tax():
    assert run("1000.00", "50.00", "", amounts("600.00", "400.00")) == ("1000.00", "50.00", {})


def test_the_rate_alone_ties_the_two():
    assert run("1000.00", "50.00", "") == ("1000.00", "50.00", {})


def test_quantities_times_prices_prove_the_net():
    assert run("1000.00", None, "", [{"qty": "4", "price": "250.00"}])[2].get("net") is None


def test_a_figure_nothing_proves_keeps_its_value_and_is_flagged():
    assert run("1000.00", "37.00", "") == ("1000.00", "37.00", {"net": "unproven", "tax": "unproven"})
    assert run("1000.00", None, "") [2]["net"] == "unproven"
    assert run(None, "37.00", "")[2]["tax"] == "unproven"


def test_a_gross_read_as_net_is_contradicted_by_the_printed_total():
    net, tax, codes = run("1050.00", "50.00", "Total 1,050.00")
    assert (net, tax) == (None, None)
    assert codes == {"net": "contradicted", "tax": "contradicted"}


def test_a_count_read_as_net_is_contradicted():
    assert run("12", "50.00", "Total 1,050.00")[:2] == (None, None)


def test_lines_keep_the_net_and_the_tax_alone_goes():
    net, tax, codes = run("1000.00", "37.00", "Total 1,050.00", amounts("600.00", "400.00"))
    # Lines read short can sum to a net as well: with the total against them the net stands, but not as proved.
    assert (net, tax) == ("1000.00", None)
    assert codes == {"net": "unproven", "tax": "contradicted"}


def test_a_charge_between_the_figures_and_the_total_contradicts_nothing_the_rate_ties():
    net, tax, codes = run("1000.00", "50.00", "Freight 25.00\nTotal 1,075.00")
    assert (net, tax) == ("1000.00", "50.00")
    assert codes == {"net": "unproven", "tax": "unproven"}


@pytest.mark.parametrize("printed", [
    "Total VAT 37.00", "Total Net 1,000.00", "Total Qty 12", "Total Items 3", "Total Discount 25.00", "Sub Total 1,000.00",
    "Total Excl VAT 1,000.00", "Total Weight 400.00", "Total 2 050.00 3 000.00", "Previous Total 900.00",
])
def test_no_gross_printed_means_nothing_to_contradict(printed):
    net, tax, codes = run("1000.00", "37.00", printed)
    assert (net, tax) == ("1000.00", "37.00")
    assert codes == {"net": "unproven", "tax": "unproven"}


def test_a_total_in_a_second_currency_still_proves():
    assert run("1000.00", "50.00", "Total AED 3,855.00\nTotal USD 1,050.00")[2] == {}


def test_a_small_rounding_is_no_contradiction():
    assert run("1000.00", "50.01", "Total 1,050.00")[2] == {}


def test_a_credit_note_is_checked_by_size():
    assert run("-1000.00", "-50.00", "Total 1,050.00")[2] == {}


def test_lines_short_of_the_net_alone_do_not_blank_it():
    assert run("1000.00", "50.00", "", amounts("600.00"))[:2] == ("1000.00", "50.00")


def test_no_figures_no_flags():
    assert run(None, None, "Total 1,050.00")[2] == {}


def test_a_tax_tiny_beside_a_misread_net_ties_to_no_rate():
    assert run("450005000", "4500.50", "Goods Value 450.005.000\nVAT 4,500.50\nAmount Due 54,505.50")[:2] == (None, None)


def test_a_net_far_over_the_printed_total_goes_even_with_no_tax():
    assert run("450005000", None, "Goods Value 450.005.000\nTotal Incl VAT 54,505.50")[0] is None


@pytest.mark.parametrize("net, tax, printed", [
    ("30", None, "Net Amount 30 Days"), ("12", None, "Subtotal 12 Total"), ("47362", None, "Sub Total TRY 47362"),
    (None, "47362", "Tax PHP 47362"), ("2", "1", "Taxable VAT Total\n2 1 3"), ("20", "1", "Taxable VAT Total\n20 1 21"),
])
def test_a_whole_number_nothing_proves_goes_blank_and_flagged(net, tax, printed):
    blank_net, blank_tax, codes = run(net, tax, printed)
    assert (blank_net, blank_tax) == (None, None)
    assert set(codes) == {field for field, value in (("net", net), ("tax", tax)) if value}


@pytest.mark.parametrize("net, tax, printed, lines", [
    ("4500", "225", "Grand Total 4725", None),
    ("4500", None, "Sub Total 4500", amounts("4000", "500")),
    ("4500", "225", "VAT 5% 225", None),
    ("0", "0", "Total 0", None),
])
def test_a_whole_number_the_page_proves_stands(net, tax, printed, lines):
    assert run(net, tax, printed, lines)[:2] == (net, tax)


def test_a_whole_value_printed_with_decimals_is_only_flagged():
    net, _, codes = run("4500", None, "Sub Total 4,500.00")
    assert net == "4500" and codes == {"net": "unproven"}


@pytest.mark.parametrize("tax", ["50.00", None])
def test_short_lines_cannot_prove_a_net_far_below_the_printed_total(tax):
    net, _, codes = run("1000.00", tax, "Sub Total 1,000.00\nGrand Total 9,450.00", amounts("1000.00"))
    assert net is None and codes["net"] == "contradicted"


def test_a_total_that_disagrees_flags_a_net_the_lines_prove():
    # Freight or a discount can explain the gap: nothing is blanked, and nothing passes as proved.
    net, tax, codes = run("1000.00", "50.00", "Grand Total 1,150.00", amounts("1000.00"))
    assert (net, tax) == ("1000.00", "50.00") and codes == {"net": "unproven", "tax": "unproven"}


def test_a_rate_does_not_outvote_a_printed_total_with_no_lines():
    assert run("9000.00", "450.00", "Grand Total 4,725.00")[:2] == (None, None)


def test_lines_short_of_the_printed_count_prove_nothing():
    _, _, codes = run("1000.00", None, "Total lines: 3", amounts("1000.00"))
    assert codes == {"net": "unproven"}


@pytest.mark.parametrize("printed", ["Sub Total 4,500", "Sub Total 4 500"])
def test_a_whole_figure_printed_with_a_separator_is_only_flagged(printed):
    net, _, codes = run("4500", None, printed)
    assert net == "4500" and codes == {"net": "unproven"}


@pytest.mark.parametrize("printed", ["Net Payable 102,941,278", "Net Due 102,941,278"])
def test_a_net_payable_is_a_printed_total(printed):
    assert run("98860673", "4080605", printed) == ("98860673", "4080605", {})


def test_a_figure_that_stays_keeps_the_flag_the_ai_already_put_on_it():
    review = {"reason": "the AI read this differently", "other_value": "800"}
    invoice = {"net": "812.50", "tax": None, "lines": []}
    evidence = {"header": {"net": {"quote": "", "page": 1, "source": "ocr", "review": review}}}
    guard_figures(invoice, evidence, {}, "ocr", "")
    assert invoice["net"] == "812.50" and evidence["header"]["net"]["review"] == review


def test_a_figure_that_goes_blank_says_why_even_if_the_ai_flagged_it():
    review = {"reason": "the AI read this differently", "other_value": "800"}
    invoice = {"net": "812.50", "tax": "81.25", "lines": []}
    evidence = {"header": {"net": {"quote": "", "page": 1, "source": "ocr", "review": review}}}
    guard_figures(invoice, evidence, {}, "ocr", "Grand Total 5,000.00")
    assert invoice["net"] is None and evidence["header"]["net"]["review"]["code"] == "contradicted"
