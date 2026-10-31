"""The scorecard's scorer, not the readers: synthetic expected/read pairs whose right answer is known.

Every wrong read here must score wrong. A scorer that calls any of them correct fails this suite. No reader runs:
the run-status tests hand run_status() process()-shaped results, and the summary tests hand summarise() runs.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("engine_scorecard", ROOT / "scripts" / "engine_scorecard.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

EXPECTED = {
    "number": "INV-1", "supplier_name": "Acme Trading LLC", "date": "2026-10-04", "po": "70001",
    "currency": "AED", "net": "800", "tax": "40.0",
    "lines": [
        {"sku": "A-1", "gtin": "00012345678905", "description": "Serum 30 ml", "qty": "10", "price": "60"},
        {"sku": "B-2", "gtin": "00012345678912", "description": "Cream 50 ml", "qty": "5", "price": "40"},
    ],
}


def read(**changes):
    """A deep copy of EXPECTED with header changes; lines=... replaces the lines."""
    actual = json.loads(json.dumps(EXPECTED))
    actual.update(changes)
    return actual


def outcomes(actual, expected=EXPECTED):
    return dict(sc.score_case(expected, actual)["fields"])


def assert_wrong_read(actual, field, outcome="wrong", expected=EXPECTED):
    """The read must be called wrong on `field`, and the case must not be exact."""
    scored = sc.score_case(expected, actual)
    assert dict(scored["fields"])[field] == outcome, scored
    assert not scored["exact"], "a wrong read was scored exact"


# ---- a perfect read, and equivalent spellings that are the same value ---------------------------------------------

def test_the_expected_itself_is_exact_on_every_field():
    scored = sc.score_case(EXPECTED, read())
    assert scored["exact"] and set(dict(scored["fields"]).values()) == {"correct"}
    assert scored["line_key"] == "sku" and (scored["lines_missing"], scored["lines_extra"]) == (0, 0)


def test_every_expected_field_is_scored_and_nothing_else():
    names = [f for f, _ in sc.score_case(EXPECTED, read(buyer_name="extra, not expected"))["fields"]]
    header = sorted(k for k in EXPECTED if k != "lines")
    line = sorted(f"lines[].{f}" for f in EXPECTED["lines"][0] for _ in EXPECTED["lines"])
    assert names == header + line + ["lines[]"]


def test_same_values_written_differently_are_correct():
    actual = read(net="800.00", tax="40", number="  inv-1 ", supplier_name="ACME  trading\tllc", date="04 Oct 2026")
    actual["lines"].reverse()  # a set: order does not matter
    actual["lines"][0]["qty"] = "5.000"
    assert sc.score_case(EXPECTED, actual)["exact"]


@pytest.mark.parametrize("printed", ["2026-10-04", "20261004", "2026-10-04T00:00:00", "2026/10/04", "4 October 2026",
                                     "Oct 4, 2026", "04-Oct-2026"])
def test_unambiguous_date_forms_normalise_to_the_same_day(printed):
    assert outcomes(read(date=printed))["date"] == "correct"


# ---- wrong reads that must score wrong ----------------------------------------------------------------------------

@pytest.mark.parametrize("net", ["800.01", "799.99", "800.001", "8000", "-800", "80"])
def test_an_amount_off_by_a_cent_or_a_fils_or_a_digit_is_wrong(net):
    assert_wrong_read(read(net=net), "net")


def test_a_line_amount_off_by_one_cent_is_wrong():
    actual = read()
    actual["lines"][1]["price"] = "40.01"
    assert_wrong_read(actual, "lines[].price")


@pytest.mark.parametrize("net", ["800 AED", "1,000", "eight hundred", "", "NaN", "Infinity"])
def test_an_amount_that_is_not_a_plain_number_is_wrong_not_skipped(net):
    assert_wrong_read(read(net=net), "net")


@pytest.mark.parametrize("printed", ["2026-04-10", "2026-10-05", "2025-10-04", "10/04/2026", "04/10/2026", "",
                                     "not a date"])
def test_a_transposed_shifted_or_ambiguous_date_is_wrong(printed):
    assert_wrong_read(read(date=printed), "date")


def test_a_different_string_is_wrong_and_a_gtin_keeps_its_leading_zeros():
    assert_wrong_read(read(number="INV-11"), "number")
    actual = read()
    actual["lines"][0]["gtin"] = "12345678905"
    assert_wrong_read(actual, "lines[].gtin")


def test_null_and_empty_string_never_match_each_other():
    assert_wrong_read(read(po=None), "po", "missing")
    assert_wrong_read(read(po=""), "po")
    assert_wrong_read(read(po="   "), "po")
    expected = {**EXPECTED, "po": None}
    assert_wrong_read(read(po=""), "po", "unexpected", expected=expected)
    expected = {**EXPECTED, "po": ""}
    assert_wrong_read(read(po=None), "po", "missing", expected=expected)
    assert outcomes(read(po=None), {**EXPECTED, "po": None})["po"] == "correct"


def test_a_field_absent_from_the_read_counts_as_null_not_as_a_pass():
    actual = read()
    del actual["currency"]
    assert_wrong_read(actual, "currency", "missing")
    actual = read()
    del actual["lines"]
    assert_wrong_read(actual, "lines[]")


def test_a_value_read_where_none_is_printed_is_unexpected():
    assert_wrong_read(read(tax="5"), "tax", "unexpected", expected={**EXPECTED, "tax": None})


def test_a_missing_line_scores_its_fields_missing_and_the_line_set_wrong():
    actual = read(lines=EXPECTED["lines"][:1])
    scored = sc.score_case(EXPECTED, actual)
    assert (scored["lines_missing"], scored["lines_extra"], scored["exact"]) == (1, 0, False)
    fields = scored["fields"]
    assert ("lines[]", "wrong") in fields
    assert sum(1 for f, o in fields if f.startswith("lines[].") and o == "missing") == len(EXPECTED["lines"][1])


def test_an_extra_line_makes_the_line_set_wrong_even_when_every_expected_line_is_right():
    extra = {"sku": "C-3", "gtin": None, "description": "Toner", "qty": "1", "price": "9"}
    scored = sc.score_case(EXPECTED, read(lines=EXPECTED["lines"] + [extra]))
    assert (scored["lines_missing"], scored["lines_extra"], scored["exact"]) == (0, 1, False)
    assert dict(scored["fields"])["lines[]"] == "wrong"


def test_a_duplicated_line_is_an_extra_line():
    scored = sc.score_case(EXPECTED, read(lines=EXPECTED["lines"] + EXPECTED["lines"][:1]))
    assert (scored["lines_extra"], scored["exact"]) == (1, False)


def test_a_line_read_under_the_wrong_key_is_missing_plus_extra_not_matched():
    actual = read()
    actual["lines"][0]["sku"] = "A-7"
    scored = sc.score_case(EXPECTED, actual)
    assert (scored["lines_missing"], scored["lines_extra"], scored["exact"]) == (1, 1, False)


def test_lines_swapped_values_under_the_right_keys_are_wrong():
    actual = read()
    actual["lines"][0]["qty"], actual["lines"][1]["qty"] = "5", "10"
    assert_wrong_read(actual, "lines[].qty")


def test_no_lines_read_when_lines_are_expected_is_wrong_and_no_lines_expected_and_read_is_correct():
    assert_wrong_read(read(lines=[]), "lines[]")
    assert outcomes(read(lines=[]), {**EXPECTED, "lines": []})["lines[]"] == "correct"
    assert_wrong_read(read(), "lines[]", expected={**EXPECTED, "lines": []})


def test_the_line_key_falls_back_to_gtin_then_description_and_is_stated():
    no_sku = {**EXPECTED, "lines": [{k: v for k, v in line.items() if k != "sku"} for line in EXPECTED["lines"]]}
    assert sc.score_case(no_sku, read(lines=no_sku["lines"]))["line_key"] == "gtin"
    only_text = {**EXPECTED, "lines": [{"description": "Serum 30 ml", "qty": "1"}]}
    assert sc.score_case(only_text, read(lines=only_text["lines"]))["line_key"] == "description"


def test_every_single_field_corruption_is_caught():
    # One corrupted field at a time, header and line: not one of them may pass as an exact read.
    for field in [k for k in EXPECTED if k != "lines"]:
        assert not sc.score_case(EXPECTED, read(**{field: "corrupted"}))["exact"], field
        assert not sc.score_case(EXPECTED, read(**{field: None}))["exact"], field
    for field in EXPECTED["lines"][0]:
        actual = read()
        actual["lines"][1][field] = "corrupted"
        assert not sc.score_case(EXPECTED, actual)["exact"], field


# ---- the corpus itself ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [{"net": "about 800"}, {"date": "04/10/2026"},
                                 {"lines": [{"qty": "1"}, {"sku": "X", "qty": "2"}]}, {"lines": "two lines"}])
def test_an_expected_json_that_breaks_the_rules_is_refused_not_scored(bad):
    with pytest.raises(sc.BadExpected):
        sc.score_case({**EXPECTED, **bad}, read())


def test_a_bad_expected_amount_on_an_unmatched_line_is_still_refused():
    expected = {**EXPECTED, "lines": [{"sku": "A-1", "qty": "ten"}]}
    with pytest.raises(sc.BadExpected):
        sc.score_case(expected, read(lines=[]))


# ---- run status: a reader that did not read is never a score ------------------------------------------------------

def result(selected="none", *trace):
    return {"selected_engine": selected, "trace": list(trace)}


@pytest.mark.parametrize("res, want", [
    (result("invoice2data", {"engine": "invoice2data", "status": "extracted"}), ("read", [])),
    (result("none", {"engine": "paddleocr", "status": "unavailable", "reason": "Not installed."}),
     ("unavailable", ["paddleocr"])),
    (result("none", {"engine": "docling", "status": "models_unavailable"}), ("unavailable", ["docling"])),
    (result("none", {"engine": "docling", "status": "failed", "ocr_status": "timed_out", "reason": "x"}),
     ("timed_out", ["docling"])),
    (result("none", {"engine": "invoice2data", "status": "failed", "reason": "Reader timed out. Try another"}),
     ("timed_out", ["invoice2data"])),
    (result("none", {"engine": "invoice2data", "status": "failed", "reason": "Reader failed"}),
     ("failed", ["invoice2data"])),
    (result("none", {"engine": "invoice2data", "status": "text_only"}), ("no_invoice", ["invoice2data"])),
    (result("none"), ("no_invoice", [])),
])
def test_run_status_names_what_happened(res, want):
    assert sc.run_status(res) == want


def test_an_unavailable_reader_is_counted_apart_and_never_folds_into_accuracy():
    good = {"status": "read", "seconds": 1.0, "exact": True, "lines_missing": 0, "lines_extra": 0,
            "fields": [("net", "correct"), ("number", "correct")]}
    runs = {"a.pdf": good, "b.png": {"status": "unavailable", "detail": ["paddleocr"], "seconds": 0.1},
            "c.png": {"status": "crashed", "detail": ["ValueError"], "seconds": 0.1}}
    s = sc.summarise("invoice2data", runs)
    assert s["accuracy"] == 1.0 and (s["read"], s["cases"]) == (1, 3)
    assert s["statuses"] == {"read": 1, "unavailable": 1, "crashed": 1}
    assert s["loses"]["b.png"] == {"status": "unavailable", "detail": ["paddleocr"]}


def test_a_reader_that_read_nothing_has_no_accuracy_rather_than_zero():
    s = sc.summarise("docling", {"a.png": {"status": "unavailable", "detail": ["docling"], "seconds": 0.0}})
    assert s["accuracy"] is None and s["read"] == 0
    assert "n/a" in sc.pct(s["accuracy"])


def test_the_chain_handover_rate_counts_fallback_and_gap_fill_only():
    def run(need, exact=True):
        return {"status": "read", "seconds": 0.0, "exact": exact, "lines_missing": 0, "lines_extra": 0,
                "fields": [("net", "correct")], "selected_engine": "invoice2data", "need": need,
                "unavailable_in_chain": ["docling"]}
    s = sc.summarise("auto", {"a": run("cross_check"), "b": run("gap_fill"), "c": run("fallback"), "d": run(None)})
    assert s["handover"] == {"count": 2, "of": 4, "rate": 0.5}
    assert s["unavailable_in_chain"] == ["docling"]


def test_a_read_run_keeps_no_invoice_content():
    run = {"status": "read", "seconds": 0.0, "invoice": read(), "selected_engine": "invoice2data", "detail": []}
    scored = sc.score_run(run, EXPECTED)
    text = json.dumps(scored)
    assert "invoice" not in scored and "Acme" not in text and "INV-1" not in text


@pytest.mark.parametrize("status", ["unavailable", "no_invoice", "timed_out", "failed"])
def test_a_run_that_did_not_read_is_left_unscored_not_scored_against_an_empty_invoice(status):
    run = {"status": status, "seconds": 0.0, "invoice": {}, "selected_engine": "none", "detail": ["docling"]}
    scored = sc.score_run(run, EXPECTED)
    assert "fields" not in scored and "exact" not in scored and "invoice" not in scored
    s = sc.summarise("docling", {"a.png": scored})
    assert (s["accuracy"], s["fields_scored"], s["read"]) == (None, 0, 0)


# ---- expected keys against the reader's output shape --------------------------------------------------------------

FIELDS = ([k for k in EXPECTED if k != "lines"] + ["buyer_name"], ["sku", "gtin", "description", "qty", "price"])


def test_an_expected_key_the_reader_never_outputs_is_refused_not_scored_missing_forever():
    with pytest.raises(sc.BadExpected, match="invoice_number"):
        sc.score_case({**EXPECTED, "invoice_number": "INV-1"}, read(), FIELDS)
    lines = [{**EXPECTED["lines"][0], "unit_price": "60"}]
    with pytest.raises(sc.BadExpected, match=r"lines\[\]\.unit_price"):
        sc.score_case({**EXPECTED, "lines": lines}, read(), FIELDS)
    assert sc.score_case(EXPECTED, read(), FIELDS)["exact"]


def test_the_local_target_names_the_apps_own_invoice_and_line_fields():
    header, line = sc.LocalChain().fields()
    assert {"number", "date", "net", "tax", "supplier_name"} <= set(header) and "lines" not in header
    assert {"sku", "gtin", "qty", "price", "net_amount"} <= set(line)


# ---- corpus layout -------------------------------------------------------------------------------------------------

def test_both_corpus_layouts_are_found_and_every_layout_problem_is_named(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "orphan.png").write_bytes(b"")
    (tmp_path / "reference.json").write_text("{}")
    good = tmp_path / "case-1"
    good.mkdir()
    (good / "scan.png").write_bytes(b"")
    (good / "expected.json").write_text("{}")
    (good / "notes.md").write_text("ignored")
    two = tmp_path / "case-2"
    two.mkdir()
    (two / "x.pdf").write_bytes(b"")
    (two / "y.pdf").write_bytes(b"")
    (two / "expected.json").write_text("{}")
    bare = tmp_path / "case-3"
    bare.mkdir()
    (bare / "x.pdf").write_bytes(b"")
    (tmp_path / "unrelated").mkdir()
    found = {c["name"]: c["layout_error"] for c in sc.find_cases(tmp_path)}
    assert list(found) == ["a.pdf", "case-1/", "case-2/", "case-3/", "orphan.png"]
    assert found["a.pdf"] is None and found["case-1/"] is None
    assert "2 documents" in found["case-2/"] and "expected.json" in found["case-3/"]
    assert "no expected JSON" in found["orphan.png"]


class FakeTarget:
    """A target whose reads are given: the runner, not a reader, is under test."""
    name = "fake"
    runs = ("invoice2data", "auto")

    def __init__(self, reads):
        self.reads = reads

    def fields(self):
        return FIELDS

    def readers(self):
        return {"invoice2data": {"installed": True, "models_blocked": False, "version": "1"}}

    def read(self, path, run):
        return self.reads[path.name]


def test_the_runner_reports_layout_and_expected_problems_as_bad_expected_and_scores_the_rest(tmp_path):
    for name, expected in (("good", EXPECTED), ("typo", {**EXPECTED, "invoice_no": "1"})):
        case = tmp_path / name
        case.mkdir()
        (case / "doc.pdf" if name == "good" else case / "other.pdf").write_bytes(b"")
        (case / "expected.json").write_text(json.dumps(expected))
    (tmp_path / "lonely").mkdir()
    (tmp_path / "lonely" / "expected.json").write_text("{}")
    reads = {"doc.pdf": {"status": "read", "seconds": 0.0, "invoice": read(net="800.01"), "detail": [],
                         "selected_engine": "invoice2data", "need": "gap_fill", "unavailable_in_chain": []},
             "other.pdf": {"status": "read", "seconds": 0.0, "invoice": read(), "detail": []}}
    report = sc.run_corpus(tmp_path, ["auto"], FakeTarget(reads))
    s = report["summary"]["auto"]
    assert s["statuses"] == {"read": 1, "bad_expected": 2}
    assert s["loses"]["good/"]["not_correct"] == ["net"]
    assert s["handover"] == {"count": 1, "of": 1, "rate": 1.0}
    assert "invoice_no" in s["loses"]["typo/"]["detail"][0]
    assert "INV-1" not in json.dumps(report) and "Acme" not in json.dumps(report)
    assert "WARNING" not in sc.table(report)
