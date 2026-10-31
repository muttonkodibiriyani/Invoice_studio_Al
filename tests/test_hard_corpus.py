"""The hard-case corpus (hard-cases/): its ground truth is what the documents print, checked without any reader.

The expected JSON is checked against the PDF's own text layer (pdfplumber), the sums are recomputed, the GTIN check
digits are checked, and the committed JSON must be exactly what scripts/build_hard_corpus.py writes from its spec,
so a hand edit to a JSON fails here. The scans' JSON must equal their printed source's. docs/HARD_CORPUS.md."""
import json
import pathlib
import sys
from decimal import Decimal

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import build_hard_corpus as corpus  # noqa: E402
import engine_scorecard  # noqa: E402

HARD = ROOT / "hard-cases"
CASES = {case["id"]: case for case in corpus.CASES}
SCANS = {scan["id"]: scan for scan in corpus.SCANS}


def committed(case_id):
    return json.loads((HARD / f"{case_id}.json").read_text())


def pdf_text(case_id):
    pdfplumber = pytest.importorskip("pdfplumber")
    with pdfplumber.open(HARD / f"{case_id}.pdf") as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def test_every_document_has_its_expected_json_and_every_json_its_document():
    documents = {p.stem for p in HARD.iterdir() if p.suffix.lower() in engine_scorecard.DOCUMENT_SUFFIXES}
    answers = {p.stem for p in HARD.glob("*.json")}
    assert documents == answers == set(CASES) | set(SCANS)
    assert len({p.stem for p in HARD.iterdir() if p.suffix != ".json"}) == len(documents)


@pytest.mark.parametrize("case_id", sorted(CASES) + sorted(SCANS))
def test_the_committed_json_is_what_the_spec_writes(case_id):
    case = CASES[SCANS[case_id]["source"]] if case_id in SCANS else CASES[case_id]
    assert committed(case_id) == corpus.expected(case), "regenerate with scripts/build_hard_corpus.py; never hand-edit"


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_every_expected_value_is_printed_on_the_page(case_id):
    text = pdf_text(case_id)
    missing = [token for token in corpus.printed_tokens(CASES[case_id]) if token not in text]
    assert not missing, f"{case_id}: the expected JSON holds values the page does not print: {missing}"


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_the_committed_header_is_printed_and_the_supplier_is_not_the_bill_to(case_id):
    """Checks the committed JSON itself against the page, so a spec field wired to the wrong JSON key fails."""
    want, text = committed(case_id), pdf_text(case_id)
    for field in ("supplier_name", "number", "po", "currency"):
        assert want[field] is None or want[field] in text, field
    case = CASES[case_id]
    labelled = [(case["number_label"], want["number"]), (case["date_label"], case["date_printed"])]
    if case["po"] or want["po"]:
        labelled.append((case["po_label"], want["po"]))
    for label, value in labelled:
        assert f"{label} {value}" in text, f"{value!r} is not the value printed beside {label!r}"
    assert want["supplier_name"] != case["buyer"]
    assert f"Bill To: {want['supplier_name']}" not in text


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_the_expected_date_is_the_printed_date_and_an_ambiguous_one_is_not_guessed(case_id):
    """date_printed is as printed; date is it read day-first, or null where the app's own rule calls it ambiguous."""
    from datetime import datetime

    from app.fine_rules import parse_printed_date

    want, printed = committed(case_id), CASES[case_id]["date_printed"]
    assert want["date_printed"] == printed and f"{CASES[case_id]['date_label']} {printed}" in pdf_text(case_id)
    if parse_printed_date(printed, None)["ambiguous"]:
        assert want["date"] is None
        return
    for fmt in ("%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            day = datetime.strptime(printed, fmt).date()
            break
        except ValueError:
            continue
    else:
        pytest.fail(f"{case_id}: printed date {printed!r} is in no known form")
    assert want["date"] == day.isoformat()


def test_the_corpus_has_both_ambiguous_and_unambiguous_numeric_dates():
    from app.fine_rules import parse_printed_date

    flags = {parse_printed_date(c["date_printed"], None)["ambiguous"] for c in CASES.values() if "/" in c["date_printed"]}
    assert flags == {True, False}


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_the_sums_hold(case_id):
    case, want = CASES[case_id], committed(case_id)
    places = case["decimals"]
    for line in want["lines"]:
        assert corpus.q(Decimal(line["qty"]) * Decimal(line["price"]), places) == Decimal(line["net_amount"])
    lines = sum(Decimal(line["net_amount"]) for line in want["lines"])
    assert lines - Decimal(case.get("discount") or 0) == Decimal(want["net"])
    if case["vat"] is None:
        assert want["tax"] is None
    else:
        assert corpus.q(Decimal(want["net"]) * Decimal(case["vat"]) / 100, places) == Decimal(want["tax"])


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_a_gtin_is_expected_only_when_its_check_digit_is_right(case_id):
    for spec, line in zip(CASES[case_id]["lines"], committed(case_id)["lines"]):
        if line["gtin"] is not None:
            assert corpus.gtin_ok(line["gtin"]) and line["gtin"] == spec["barcode"]
        elif spec["barcode"]:
            assert not corpus.gtin_ok(spec["barcode"])


def test_the_bad_barcode_case_really_prints_a_barcode_that_fails_its_check():
    bad = [line["barcode"] for line in CASES["h10-bad-barcode"]["lines"] if not corpus.gtin_ok(line["barcode"])]
    assert len(bad) == 1 and bad[0] in pdf_text("h10-bad-barcode")


def test_gtin_check_digit_on_known_codes():
    assert corpus.gtin_ok("00012345678905") and corpus.gtin_ok("4006381333931")
    assert not corpus.gtin_ok("4006381333932") and not corpus.gtin_ok("00012345678906")


@pytest.mark.parametrize("case_id", sorted(CASES) + sorted(SCANS))
def test_the_scorecard_accepts_the_expected_json(case_id):
    """No case is bad_expected: every amount parses, the date is unambiguous, and lines have a key."""
    want = committed(case_id)
    for field, value in want.items():
        if field != "lines":
            engine_scorecard.check_expected(field, value)
    for line in want["lines"]:
        for field, value in line.items():
            engine_scorecard.check_expected(field, value)
    assert any(all(line[key] for line in want["lines"]) for key in engine_scorecard.LINE_KEYS)


@pytest.mark.parametrize("case_id", sorted(s for s in SCANS if SCANS[s]["format"] == "pdf"))
def test_a_scanned_pdf_has_no_text_layer(case_id):
    pdfplumber = pytest.importorskip("pdfplumber")
    with pdfplumber.open(HARD / f"{case_id}.pdf") as pdf:
        assert not "".join(page.extract_text() or "" for page in pdf.pages).strip()
        assert len(pdf.pages) == len(pdfplumber.open(HARD / f"{SCANS[case_id]['source']}.pdf").pages)


def test_the_two_page_case_puts_rows_and_totals_on_page_two():
    pdfplumber = pytest.importorskip("pdfplumber")
    with pdfplumber.open(HARD / "h05-two-page-table.pdf") as pdf:
        assert len(pdf.pages) == 2
        second = pdf.pages[1].extract_text()
    assert "Subtotal" in second and "Item Code" in second and "OHE-345" in second
