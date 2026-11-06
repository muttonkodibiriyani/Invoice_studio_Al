"""The fast part of the invoice simulation (docs/INVOICE_SIM.md): no OCR models. The real generator, a real text-layer read,
the real rule chain on a master activated through the real API, and the real export workbook. Nothing here is stubbed."""
import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("reportlab")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import engines  # noqa: E402
from app.models import ProcessingOptions  # noqa: E402
from tools.invoice_sim import ai, flow  # noqa: E402
from tools.invoice_sim import run as sim  # noqa: E402
from tools.invoice_sim.generator import SEED, build_corpus, render_pdf  # noqa: E402
from tools.invoice_sim.master import parent_id  # noqa: E402
from tools.invoice_sim.score import align, score_read, verdict_header, verdict_line  # noqa: E402


@pytest.fixture(scope="module")
def corpus():
    return build_corpus(SEED, 5)


@pytest.fixture(scope="module")
def rig(corpus, tmp_path_factory):
    made = sim.RulesRig(corpus, tmp_path_factory.mktemp("rig"))
    yield made
    made.close()


def truth_of(corpus, invoice_id):
    return json.loads(json.dumps(next(i for i in corpus.invoices if i.id == invoice_id).truth, default=str))


def test_the_corpus_is_large_and_the_same_every_time(corpus):
    assert len(corpus.invoices) >= 200
    assert len({i.family for i in corpus.invoices}) >= 40
    again = build_corpus(SEED, 5)
    fingerprint = lambda c: hashlib.sha256(json.dumps([i.truth for i in c.invoices], default=str, sort_keys=True).encode()).hexdigest()  # noqa: E731
    assert fingerprint(corpus) == fingerprint(again)
    assert fingerprint(corpus) != fingerprint(build_corpus(SEED + 1, 5))


def test_every_item_class_is_in_the_corpus(corpus):
    seen = {line["item_class"] for i in corpus.invoices for line in i.truth["lines"]}
    assert {"in_master_code", "in_master_barcode", "not_in_master", "ambiguous_code", "ambiguous_barcode",
            "other_supplier_code", "other_supplier_barcode"} <= seen


def test_ai_cases_share_one_real_ocr_read(tmp_path):
    calls = []

    def real(engine, path, root, language="en", extra=(), timeout=None):
        calls.append(engine)
        if len(calls) == 1:
            raise TimeoutError("first read timed out")
        return {"read": len(calls)}
    engines = SimpleNamespace(local_read=real)
    ai.steady_local_reads(engines)
    page = tmp_path / "a.pdf"
    page.write_bytes(b"one")
    # A failure is not kept: the next case reads for real, then that answer stands.
    with pytest.raises(TimeoutError):
        engines.local_read("paddleocr", page, tmp_path)
    assert engines.local_read("paddleocr", page, tmp_path) == {"read": 2}
    assert engines.local_read("paddleocr", page, tmp_path) == {"read": 2}
    # Another file, and a reader that is not OCR, still read for real.
    page.write_bytes(b"two")
    assert engines.local_read("paddleocr", page, tmp_path) == {"read": 3}
    assert engines.local_read("invoice2data", page, tmp_path) == {"read": 4}
    assert engines.local_read("invoice2data", page, tmp_path) == {"read": 5}


def test_file_verdict_says_what_a_user_meets():
    from tools.invoice_sim.files import verdict
    read = {"status": "review", "number": "A1", "lines": 2, "told": ["Check the date"]}
    assert verdict({"upload_status": 200, "jobs": [read], "export": [{"status": 200}]}) == "read, review"
    assert verdict({"upload_status": 400}) == "refused"
    # A 401 is the harness's expired sign-in, not the app refusing the file.
    assert verdict({"upload_status": 401}) == "NOT RUN, SIGN-IN EXPIRED"
    assert verdict({"upload_status": 200, "jobs": [read], "export": [{"status": 500}]}) == "EXPORT CRASH"
    assert verdict({"upload_status": 200, "jobs": [read], "invoices_in_file": 2, "export": [{"status": 200}]}) == "INVOICES LOST, NOT TOLD"
    # "Several" about something else is no message about the lost invoices.
    other = {**read, "told": ["Printed supplier name has several Supplier CODEs"]}
    assert verdict({"upload_status": 200, "jobs": [other], "invoices_in_file": 2, "export": [{"status": 200}]}) == "INVOICES LOST, NOT TOLD"
    told = {**read, "told": ["This file holds several invoices"]}
    assert verdict({"upload_status": 200, "jobs": [told], "invoices_in_file": 2, "export": [{"status": 200}]}) == "read, review"
    assert verdict({"upload_status": 200, "jobs": [{**read, "number": None, "lines": 0, "told": []}], "export": []}) == "EMPTY SUCCESS"


def test_verdicts_tell_blank_from_wrong():
    truth = {"number": "A1", "net": "10.00", "tax": "1.50", "tax_printed": True, "net_printed": True, "date": "2026-01-02", "date_printed": "02/01/2026"}
    assert verdict_header("number", {"number": "a-1"}, truth) == "correct"
    assert verdict_header("number", {"number": None}, truth) == "blank"
    assert verdict_header("number", {"number": "A7"}, truth) == "WRONG"
    assert verdict_header("net", {"net": "10"}, truth) == "correct"
    assert verdict_header("net", {"net": "100.00"}, truth) == "WRONG"
    assert verdict_header("date", {"date": "2026-02-01"}, truth) == "WRONG"
    assert verdict_header("tax", {"tax": "1.5"}, {**truth, "tax_printed": False}) == "WRONG"
    assert verdict_line("qty", "2.0", "2") == "correct"
    assert verdict_line("qty", "", "2") == "blank"
    assert verdict_line("sku", "ABC", "ABD") == "WRONG"


def test_alignment_finds_a_dropped_row_and_an_invented_one():
    truth = [{"sku": f"S{n}", "description": f"Thing {n}", "net_amount": str(n)} for n in range(1, 5)]
    read = [truth[0], truth[1], truth[3], {"sku": "ZZ9", "description": "Nothing like it", "net_amount": "77"}]
    pairs, dropped, phantom = align(read, truth)
    assert pairs == [(0, 0), (1, 1), (2, 3)] and dropped == [2] and phantom == [3]


def test_a_correct_read_scores_clean_through_the_real_rules_and_workbook(corpus, rig):
    truth = truth_of(corpus, "grid-classic-00")
    read = sim._truth_invoice(truth)
    view, rows = rig.run(read, truth["supplier_name"], "", [])
    scored = score_read(truth, {"invoice": read}, view, view)
    assert not scored["wrong"] and scored["line_verdict"] == "correct"
    assert len(rows) == len(truth["lines"])
    items, _, wrong = sim.score_items(truth, read["lines"], scored["pairs"], scored["phantom"], view, rows)
    assert not wrong and sum(c["WRONG"] + c["blank"] for c in items.values()) == 0


def test_item_columns_in_the_export_are_the_truth_item_or_blank(corpus, rig):
    truth = truth_of(corpus, "grid-classic-00")
    _, rows = rig.run(sim._truth_invoice(truth), truth["supplier_name"], "", [])
    classes = set()
    for row, line in zip(rows, truth["lines"]):
        classes.add(line["item_class"])
        got = str(row["Item"] or "")
        assert got == (parent_id(line["item_id"]) if line["item_id"] else ""), line["item_class"]
    assert {"in_master_code", "ambiguous_code", "not_in_master"} <= classes


def test_a_tampered_read_is_caught_and_a_silent_wrong_stays_silent(corpus, rig):
    truth = truth_of(corpus, "grid-classic-01")
    text = " ".join(str(truth[k]) for k in ("number", "net", "tax", "po", "date"))
    good = sim._truth_invoice(truth)
    baseline, _ = rig.run(good, truth["supplier_name"], text, [])
    bad = copy.deepcopy(good)
    bad["number"] = "X" + bad["number"]
    bad["tax"] = str(float(bad["tax"]) + 7)
    bad["lines"] = bad["lines"][:-1]
    bad["lines"][0]["qty"] = "9999"
    view, _ = rig.run(bad, truth["supplier_name"], text, [])
    scored = score_read(truth, {"invoice": bad}, view, baseline)
    wrong = {w["field"]: w["flagged"] for w in scored["wrong"]}
    # The number has no check that could catch it; the app's issues name the tax. This is what "WRONG no flag" means.
    assert wrong == {"number": False, "tax": True}
    assert scored["line_count"]["dropped"] == 1 and scored["line_verdict"] == "WRONG"
    assert scored["lines"]["qty"]["WRONG"] == 1 and scored["lines"]["qty"]["silent"] == 0


def test_a_generated_pdf_goes_through_the_real_reader_chain(corpus, tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_LEARN", "0")
    invoice = next(i for i in corpus.invoices if i.id == "grid-classic-00")
    path = render_pdf(invoice, tmp_path / "doc.pdf")
    (tmp_path / "work").mkdir()
    (tmp_path / "templates").mkdir()

    def no_ai(*_):
        raise AssertionError("AI is off")

    result = engines.process(Path(path), ProcessingOptions(ai_fallback=False), SimpleNamespace(root=tmp_path), no_ai)
    assert invoice.truth["number"] in (result.get("text") or "")
    scored = score_read(json.loads(json.dumps(invoice.truth, default=str)), result)
    assert set(scored["fields"]) == {"number", "supplier_name", "date", "po", "currency", "net", "tax"}
    assert all(v["verdict"] in ("correct", "blank", "WRONG") for v in scored["fields"].values())


def test_the_flow_compare_reports_a_changed_cell_and_a_dropped_row(corpus):
    truth = truth_of(corpus, "grid-classic-00")
    item = parent_id(truth["lines"][0]["item_id"]) if truth["lines"][0]["item_id"] else None
    header = {"Document": truth["number"], "Supplier Site": "S1", "Order No": truth["po"], "Location": flow.LOCATION, "Location Type": "Store (S)",
              "Document Date": truth["date"], "Total Cost Ex Tax": truth["net"], "Tax Amount": truth["tax"]}
    rows = [{"Item": parent_id(x["item_id"]) if x["item_id"] else None, "UPC": None, "Unit Cost": x["price"], "Quantity": x["qty"]} for x in truth["lines"]]
    assert flow.compare({"Header": [header], "Details": rows}, truth, "S1")["wrong"] == []

    changed = flow.compare({"Header": [{**header, "Tax Amount": "999999.99"}], "Details": rows[:-1]}, truth, "S1")["wrong"]
    assert {(x["sheet"], x["column"]) for x in changed} == {("Header", "Tax Amount"), ("Details", "row count")}
    assert flow.compare({"Header": [header], "Details": [{**rows[0], "Item": "999999999"}, *rows[1:]]}, truth, "S1")["wrong"][0]["truth"] == item


def test_the_ai_mark_is_read_from_what_the_review_screen_uses():
    job = {"readers": {"header": {"tax": "ai", "net": "regex"}, "lines": [{"price": "ai", "qty": "regex"}, {"qty": "regex"}, {"item_id": "rule:I1"}]}}
    assert ai.mark(job, "Header", "Tax Amount") == "ai"
    assert ai.mark(job, "Header", "Document") == "from ai"
    assert ai.mark(job, "line 1", "Unit Cost") == "ai"
    assert ai.mark(job, "line 1", "Item") == "from ai"
    assert ai.mark(job, "line 2", "Unit Cost") == ""
    assert ai.mark(job, "line 3", "Item") == ""
    assert ai.mark({"readers": {"header": {"net": "regex"}}}, "Header", "Tax Amount") == ""


def test_an_ai_failure_at_the_http_layer_reaches_the_user_and_nothing_else_is_called(corpus, tmp_path, monkeypatch):
    import app.providers as providers

    monkeypatch.setenv("INV_STUDIO_AZURE_ENDPOINT", f"https://{ai.HOST}")
    monkeypatch.setenv("INV_STUDIO_AZURE_API_KEY", "sim-key-not-real")
    monkeypatch.setenv("INV_STUDIO_AZURE_DEPLOYMENT", ai.MODEL)
    monkeypatch.setattr(providers, "httpx", providers.httpx)  # restored after the test; install replaces it
    foundry = ai.Foundry()
    ai.install(foundry)
    invoice = next(i for i in corpus.invoices if i.id == "grid-classic-00")
    foundry.case, foundry.truth = "500", truth_of(corpus, invoice.id)
    made = sim.RulesRig(corpus, tmp_path / "app")
    try:
        job = ai.upload(made, "doc.pdf", Path(render_pdf(invoice, tmp_path / "doc.pdf")).read_bytes(), ai.AI_ON)
    finally:
        made.close()
    assert foundry.calls >= 1 and foundry.foreign == []
    assert "HTTP 500" in str(ai.told(job))


def test_pick_document_offers_a_wrong_pick_the_old_documents_never_did(corpus):
    """Two pick cases once passed having picked nothing: the local lines held their items, or held nothing a rule could use."""
    made = copy.deepcopy(corpus)
    invoice, planted = ai.pick_document(made)
    assert planted == {0: "same supplier", 1: "another supplier"}
    for n in planted:
        line = invoice.truth["lines"][n]
        assert not line["item_id"] and not line["sku"] and not line["gtin"] and line["description"]
    spare = next(r for r in made.master if r["item_id"] == "SIM99001")
    assert spare["supplier"] == invoice.truth["supplier_name"]
    request = {"input": [{"content": [{"text": "<item_master_data>" + json.dumps([{"id": "c1", "line": 0, "fields": {"item_id": ai.parent_id(spare["item_id"])}}]) + "</item_master_data>"}]}]}
    truth = json.loads(json.dumps(invoice.truth, default=str))
    assert ai._wrong_pick(request, truth) == [{"candidate_id": "c1", "field": "item_id", "why": "looks closest"}]


def _view(header=None, lines=None, disputes=(), status="partial", why=""):
    return {"status": status, "label": status, "why": why, "counts": {}, "header": header or {}, "lines": lines or [], "disputes": [{"key": k} for k in disputes]}


def test_marks_are_judged_against_the_reads_not_the_apps_record():
    truth = {"number": "A1", "net": "100", "lines": [{"qty": "2"}]}
    local = {"number": "A1", "net": None, "lines": [{"qty": "2"}]}
    answer = {"number": "A1", "net": "100", "lines": [{"qty": "2"}]}
    invoice = {"number": "A1", "net": "100", "lines": [{"qty": "2"}]}
    honest = _view({"number": "confirmed", "net": "filled"}, [{"qty": "confirmed"}])
    assert ai.check_marks(honest, invoice, local, answer, truth, [])["false_marks"] == []
    # A blank local cell called confirmed, and a dispute nobody raised.
    lying = _view({"number": "confirmed", "net": "confirmed"}, [{"qty": "confirmed"}])
    assert [x["field"] for x in ai.check_marks(lying, invoice, local, answer, truth, [])["false_marks"]] == ["net"]
    differs = {**answer, "number": "A9"}
    silent = ai.check_marks(honest, invoice, local, differs, truth, [])
    assert [x["field"] for x in silent["false_marks"]] == ["number"]
    assert silent["wrong_ai"][0]["stopped"] is False
    stopped = ai.check_marks(_view({"net": "filled"}, [{"qty": "confirmed"}], ["number"]), invoice, local, differs, truth, [])
    assert stopped["false_marks"] == [] and stopped["wrong_ai"][0]["stopped"] is True
    # A failed AI read: anything but "not confirmed, and why" is a false mark.
    assert ai.check_marks(_view(status="not_confirmed", why="the AI read failed"), invoice, local, None, truth, [])["false_marks"] == []
    assert ai.check_marks(_view({"number": "confirmed"}, status="partial"), invoice, local, None, truth, [])["false_marks"]
    # The workbook's own comment against the view.
    mark = {"sheet": "Header", "row": 2, "column": 8, "says": "AI confirmed", "value": 100}
    assert ai.check_marks(honest, invoice, local, answer, truth, [mark])["book_false"][0]["view"] == "filled"

