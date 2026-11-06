"""Owner order, 6 Nov: every invoice gets an AI read when Foundry is on, compared afterwards. All with a stubbed AI;
Foundry itself stays unproven until Azure."""
# ruff: noqa: F811
import io
import json

import httpx
import pytest
from openpyxl import load_workbook

from app import ai_check, ai_context, ai_rules, costs
from app.excel import MARKS, rules_workbook
from app.location_type import STORE
from app.providers import Providers
from app.store import Store
from fastapi.testclient import TestClient
from tests.conftest import HEADERS, bearer
from tests.test_ai_fallback import Source, read, sample_ai, store  # noqa: F401
from tests.test_azure_foundry import FakeChatGPT, completed, options

FOUNDRY = {"provider": "azure"}
USAGE = {"input_tokens": 1000, "output_tokens": 200, "input_tokens_details": {"cached_tokens": 100}}


def whole():
    return sample_ai().model_dump(mode="json")


def check(result):
    return ai_check.view(result["readers"], result["invoice"])


# The read

def test_a_complete_local_read_still_gets_one_ai_read_on_foundry(read):
    result, calls, _ = read(local=whole(), **FOUNDRY)
    assert calls == ["ai"] and result["readers"]["ai"]["status"] == "cross_check"
    shown = check(result)
    assert shown["status"] == "confirmed" and shown["disputes"] == [] and shown["header"]["net"] == "confirmed"
    assert shown["lines"][0]["qty"] == "confirmed"


def test_another_provider_is_not_sent_a_complete_invoice(read):
    result, calls, _ = read(local=whole(), provider="openai")
    assert calls == [] and check(result)["status"] == "not_confirmed"


def test_the_ai_is_not_told_the_local_values(tmp_path, store, monkeypatch):
    # What the real provider code sends Foundry, with a local read of values printed nowhere else.
    local = {"number": "LOCALNUMBER77", "supplier_name": "Localonly Supplier", "po": "LOCALPO88", "net": "98765.43", "tax": "4321.09",
             "date": "2031-02-03", "lines": [{"sku": "LOCALSKU99", "qty": "7777", "price": "8888.25", "description": "Localonly row words"}]}
    sent = {}

    def post(self, url, **kwargs):
        sent.update(kwargs["json"])
        return httpx.Response(200, json=completed(), request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.Client, "post", post)
    azure = Providers(store, FakeChatGPT())
    azure.azure.save(azure.azure.prepare("https://acme.openai.azure.com", "a-key-0123456789", "reader"))
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    azure.extract(pdf, "Printed text only", options(), context=ai_context.build(Source(), local))
    body = json.dumps(sent)
    assert sent and "Printed text only" in body
    for value in ("LOCALNUMBER77", "Localonly Supplier", "LOCALPO88", "98765.43", "4321.09", "LOCALSKU99", "7777", "8888.25", "Localonly row"):
        assert value not in body


# The comparison

def test_agreement_is_by_normalised_value():
    assert ai_check.same("net", "600.0", "600.00") and ai_check.same("number", "INV-1", "inv 1")
    assert ai_check.same("uom", "PCS", "pcs") and ai_check.same("po", "١٢٣", "123")
    assert not ai_check.same("net", "600", "600.01") and not ai_check.same("net", "600", "x")


def test_two_different_arabic_names_are_not_the_same():
    assert not ai_check.same("supplier_name", "شركة الف", "مؤسسة باء")
    assert ai_check.same("supplier_name", "شركة الف", "شركة  الف")


def test_a_disagreement_keeps_the_local_value_and_shows_both(read):
    result, _, _ = read(local=whole(), ai=sample_ai(net=601.0, number="INV-9"), **FOUNDRY)
    assert result["invoice"]["number"] == "INV-1"
    assert float(result["invoice"]["net"]) == 600.0, "nothing is overwritten"
    shown = check(result)
    assert shown["status"] == "disputed"
    assert {x["key"]: x["ai"] for x in shown["disputes"]}.keys() == {"net", "number"}
    disputed = next(x for x in shown["disputes"] if x["key"] == "number")
    assert (disputed["local"], disputed["ai"]) == ("INV-1", "INV-9")
    assert [i["code"] for i in ai_check.issues(shown)] == [ai_check.ISSUE] * 2
    assert all(i["blocking"] and i["level"] == "hard" and "INV-9" not in i["message"] for i in ai_check.issues(shown))


def test_a_blank_local_cell_takes_the_ai_value_as_ai_filled(read):
    local = {**whole(), "date": None, "date_printed": None}
    result, _, _ = read(local=local, **FOUNDRY)
    assert result["invoice"]["date"] == "2026-10-04" and result["readers"]["header"]["date"] == "ai"
    shown = check(result)
    assert shown["header"]["date"] == "filled" and shown["header"]["net"] == "confirmed"
    assert shown["status"] == "partial", "an invoice with an AI-only value is not wholly confirmed"


def test_a_differing_description_is_not_a_dispute_or_a_confirmation():
    record = ai_check.compare({"lines": [{"description": "Serum 30ml", "qty": "1"}]}, {"lines": [{"description": "Serum 30 ml box", "qty": "1"}]},
                              {"lines": [{"description": "Serum 30ml", "qty": "1"}]}, {}, [{}])
    shown = ai_check.view({"ai_check": record}, {"lines": [{"description": "Serum 30ml", "qty": "1"}]})
    assert shown["lines"][0] == {"description": "unchecked", "qty": "confirmed"}
    assert shown["status"] == "partial" and shown["disputes"] == []


def test_a_printed_date_in_another_format_is_no_dispute_when_the_date_agrees():
    one, other = {"date": "2026-10-04", "date_printed": "04 Oct 2026"}, {"date": "2026-10-04", "date_printed": "04/10/2026"}
    assert ai_check.disputes(ai_check.compare(one, other, one, {}, [])) == []
    other = {"date": "2026-10-05", "date_printed": "05/10/2026"}
    assert {x["key"] for x in ai_check.disputes(ai_check.compare(one, other, one, {}, []))} == {"date", "date_printed"}


def test_different_line_counts_are_one_dispute_holding_both_reads(read):
    extra = {"description": "Another row", "sku": "NW-200", "qty": 1, "price": 1.0, "net_amount": 1.0}
    ai = sample_ai(lines=[*whole()["lines"], extra])
    result, _, _ = read(local=whole(), ai=ai, **FOUNDRY)
    shown = check(result)
    [dispute] = shown["disputes"]
    count = len(whole()["lines"])
    assert dispute["key"] == "lines" and (dispute["local"], dispute["ai"]) == (f"{count} lines", f"{count + 1} lines")
    assert len(dispute["ai_lines"]) == count + 1 and len(result["invoice"]["lines"]) == count


def test_an_ai_value_the_document_does_not_print_is_still_a_dispute(read):
    # Not written and not confirmed: the person sees the local value beside what the AI said.
    result, _, _ = read(local=whole(), ai=sample_ai(po="NOT-PRINTED-PO"), **FOUNDRY)
    assert result["invoice"]["po"] == whole()["po"]
    assert "po" in {x["key"] for x in check(result)["disputes"]}


# No read to compare

@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timed out"), ValueError("AI returned an invalid invoice structure"),
                                     RuntimeError("provider down")], ids=["timed-out", "malformed", "failed"])
def test_a_failed_read_finishes_not_confirmed_after_one_call(read, failure):
    result, calls, _ = read(local=whole(), ai=failure, **FOUNDRY)
    assert calls == ["ai"], "no retry"
    assert result["readers"]["ai"]["status"] == "failed" and result["invoice"]["number"] == "INV-1"
    shown = check(result)
    assert shown["status"] == "not_confirmed" and shown["label"] == "Not AI-confirmed" and shown["why"] == ai_check.NOT_READ["failed"]
    assert shown["header"] == {} and shown["lines"] == [] and ai_check.issues(shown) == []


@pytest.mark.parametrize("settings,why", [({"ai_fallback": False}, "off"), ({"model": ""}, "skipped")])
def test_with_the_ai_off_nothing_is_called_or_reads_as_confirmed(read, settings, why):
    result, calls, _ = read(local=whole(), **FOUNDRY, **settings)
    shown = check(result)
    assert calls == [] and shown["status"] == "not_confirmed" and shown["why"] == ai_check.NOT_READ[why]


def test_the_ai_as_only_reader_confirms_nothing(read):
    result, calls, _ = read(engine="ai", **FOUNDRY)
    assert calls == ["ai"] and check(result)["status"] == "not_confirmed" and check(result)["why"] == ai_check.NOT_READ["selected"]


def test_a_job_read_before_this_is_not_confirmed():
    assert ai_check.view({}, {"number": "A"})["status"] == "not_confirmed"
    assert ai_check.view(None, {})["status"] == "not_confirmed"


# Marks follow the value

def record_for(local, ai, final=None, readers=None):
    return {"ai_check": ai_check.compare(local, ai, final or local, readers or {}, [{} for _ in (final or local).get("lines", [])])}


def test_an_edited_cell_stops_showing_confirmed():
    readers = record_for({"number": "A1", "net": "5"}, {"number": "A1", "net": "5"})
    assert ai_check.view(readers, {"number": "A1", "net": "5"})["status"] == "confirmed"
    shown = ai_check.view(readers, {"number": "A2", "net": "5"})
    assert "number" not in shown["header"] and shown["status"] == "partial"


def test_a_removed_row_drops_the_line_marks_but_not_a_dispute():
    rows = [{"qty": "1", "sku": "A"}, {"qty": "2", "sku": "B"}]
    readers = record_for({"lines": rows}, {"lines": [rows[0], {"qty": "3", "sku": "B"}]})
    shown = ai_check.view(readers, {"lines": rows[:1]})
    assert shown["lines"][0] == {} and shown["lines"][1] == {"qty": "disputed"}
    assert [x["key"] for x in shown["disputes"]] == ["line 2 qty"]
    settled = ai_check.settle(readers, {"line 2 qty": "ai"}, {"lines": rows[:1]}, "someone", "now")
    assert ai_check.view(settled, {"lines": rows[:1]})["disputes"] == []


# Settling

DISPUTED = record_for({"number": "A1", "net": "5"}, {"number": "A1", "net": "6"})


def test_a_pick_settles_only_with_the_value_picked():
    with pytest.raises(ValueError, match="does not hold"):
        ai_check.settle(DISPUTED, {"net": "ai"}, {"number": "A1", "net": "5"}, "someone", "now")
    settled = ai_check.settle(DISPUTED, {"net": "ai"}, {"number": "A1", "net": "6"}, "someone", "now")
    shown = ai_check.view(settled, {"number": "A1", "net": "6"})
    assert shown["disputes"] == [] and shown["header"]["net"] == "settled" and shown["status"] == "partial"
    assert settled["ai_check"]["settled"]["net"] == {"choice": "ai", "value": "6", "by": "someone", "at": "now"}
    assert DISPUTED["ai_check"]["settled"] == {}, "the stored record is copied, not changed"


@pytest.mark.parametrize("picks", [{"number": "ai"}, {"nothing": "ai"}, {"net": "whichever"}])
def test_a_pick_must_name_an_open_dispute_and_a_known_choice(picks):
    with pytest.raises(ValueError, match="not an open AI dispute"):
        ai_check.settle(DISPUTED, picks, {"number": "A1", "net": "6"}, "someone", "now")


def test_an_edit_alone_does_not_settle_a_dispute():
    assert [x["key"] for x in ai_check.view(DISPUTED, {"number": "A1", "net": "6"})["disputes"]] == ["net"]


def test_picking_a_line_read_needs_its_row_count():
    local, ai = {"lines": [{"qty": "1"}]}, {"lines": [{"qty": "1"}, {"qty": "2"}]}
    readers = record_for(local, ai)
    with pytest.raises(ValueError, match="do not match"):
        ai_check.settle(readers, {"lines": "ai"}, local, "someone", "now")
    assert ai_check.view(ai_check.settle(readers, {"lines": "local"}, local, "someone", "now"), local)["disputes"] == []
    assert ai_check.view(ai_check.settle(readers, {"lines": "ai"}, ai, "someone", "now"), ai)["disputes"] == []


# The route: export blocked until a person picks

def job_with(store, tmp_path, jid="dis", readers=None, invoice=None):
    source = tmp_path / f"{jid}.pdf"
    source.write_bytes(b"%PDF-1.4")
    invoice = invoice or {"number": "A1", "net": "5", "tax": "1", "date": "2026-10-04", "lines": [{"sku": "ABC-1", "qty": "1", "price": "5"}]}
    store.job(jid, {"id": jid, "filename": f"{jid}.pdf", "path": str(source), "status": "review", "revision": 1, "reviewed": True,
                    "provenance": [], "invoice": invoice, "readers": readers if readers is not None else record_for(invoice, {**invoice, "net": "6"})})
    return invoice


def review(client, owner, jid, invoice, revision, **extra):
    return client.post(f"/api/jobs/{jid}/review", headers=bearer(owner), json={"invoice": invoice, "revision": revision, "confirm": True, **extra})


def test_a_dispute_blocks_the_export_and_a_confirm_does_not_clear_it(client, owner, app, tmp_path):
    invoice = job_with(app.state.store, tmp_path)
    response = review(client, owner, "dis", invoice, 1)
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["rules"]["ai_check"]["status"] == "disputed" and {"local": "5", "ai": "6"}.items() <= job["rules"]["ai_check"]["disputes"][0].items()
    assert [i for i in job["validation"]["issues"] if i["code"] == ai_check.ISSUE][0]["blocking"] is True
    refused = client.post("/api/jobs/dis/export", headers=bearer(owner), json={"revision": job["revision"]})
    assert refused.status_code == 409 and "differ" in refused.json()["detail"]
    batch = client.post("/api/exports/batch/check", headers=bearer(owner), json={"jobs": [{"id": "dis", "revision": job["revision"]}]})
    assert batch.status_code == 200 and "AI Dispute" in batch.text


def test_a_person_picks_and_the_export_goes_out_saying_so(client, owner, app, tmp_path):
    invoice = job_with(app.state.store, tmp_path)
    wrong = review(client, owner, "dis", invoice, 1, settled={"net": "ai"})
    assert wrong.status_code == 422 and "does not hold" in wrong.json()["detail"]
    picked = review(client, owner, "dis", {**invoice, "net": "6"}, 1, settled={"net": "ai"})
    assert picked.status_code == 200, picked.text
    job = picked.json()
    assert job["rules"]["ai_check"]["disputes"] == [] and job["rules"]["ai_check"]["header"]["net"] == "settled"
    assert job["readers"]["ai_check"]["settled"]["net"]["choice"] == "ai" and job["readers"]["ai_check"]["settled"]["net"]["by"]
    done = client.post("/api/jobs/dis/export", headers=bearer(owner), json={"revision": job["revision"]})
    assert done.status_code == 200, done.text
    with app.state.store.connection() as c:
        receipt = json.loads(c.execute("SELECT payload FROM exports WHERE job_id='dis'").fetchone()[0])
    assert receipt["ai_check"]["status"] == "partial" and receipt["ai_check"]["settled"] == 1


def agreed_export(client, owner, app, tmp_path, jid, site):
    invoice = {"number": "A1", "net": "5", "tax": "1", "date": "2026-10-04", "lines": [{"sku": "ABC-1", "qty": "1", "price": "5"}]}
    job_with(app.state.store, tmp_path, jid, readers=record_for(invoice, invoice), invoice=invoice)
    job = client.get(f"/api/jobs/{jid}", headers=bearer(owner)).json()
    assert job["rules"]["ai_check"]["status"] == "confirmed"
    if site:
        stored = app.state.store.job(jid)
        stored["rules"]["fields"]["site"] = {**stored["rules"]["fields"]["site"], "value": site}
        app.state.store.job(jid, stored)
    return client.post(f"/api/jobs/{jid}/export", headers=bearer(owner), json={"revision": job["revision"]})


def test_ai_agreement_does_not_clear_a_duplicate(client, owner, app, tmp_path):
    assert agreed_export(client, owner, app, tmp_path, "one", "501").status_code == 200
    assert agreed_export(client, owner, app, tmp_path, "two", "501").status_code == 409


def test_ai_agreement_does_not_clear_a_possible_duplicate(client, owner, app, tmp_path):
    assert agreed_export(client, owner, app, tmp_path, "one", None).status_code == 200
    assert agreed_export(client, owner, app, tmp_path, "two", None).status_code == 200
    with app.state.store.connection() as c:
        receipt = json.loads(c.execute("SELECT payload FROM exports WHERE job_id='two'").fetchone()[0])
    assert "Possible Duplicate" in [hold["code"] for hold in receipt["open_holds"]]


# The workbook

def workbook_of(client, owner, app, tmp_path, readers, invoice):
    job_with(app.state.store, tmp_path, readers=readers, invoice=invoice)
    view = client.get("/api/jobs/dis", headers=bearer(owner)).json()["rules"]
    for name in ("number", "po", "date", "net", "tax"):
        view["fields"][name].update(value=invoice.get(name) or view["fields"][name]["value"], flagged=False, evidence=[{"kind": "document"}])
    for line in view["lines"]:
        for name, value in (("Unit Cost", "5"), ("Quantity", "1")):
            line["cells"][name].update(value=value, flagged=False, evidence=[{"kind": "document"}])
    view["ai_check"]["written"] = ai_check.written(readers, view["ai_check"], view)
    return load_workbook(io.BytesIO(rules_workbook([view], "empty", blanks=True)[0]))


def fill(cell):
    return cell.fill.fgColor.rgb[-6:]


def test_the_excel_shows_confirmed_filled_and_disputed_cells(client, owner, app, tmp_path):
    local = {"number": "A1", "net": "5", "tax": None, "date": "2026-10-04", "lines": [{"sku": "ABC-1", "qty": "1", "price": "5"}]}
    ai = {**local, "tax": "1", "date": "2026-10-05", "lines": [{"sku": "ABC-1", "qty": "1", "price": "9"}]}
    final = {**local, "tax": "1"}
    readers = {"ai_check": ai_check.compare(local, ai, final, {"tax": "ai"}, [{}])}
    book = workbook_of(client, owner, app, tmp_path, readers, final)
    header, details = book["Header"], book["Details"]
    assert fill(header["B2"]) == MARKS["confirmed"][0] and header["B2"].comment.text.startswith("AI confirmed")
    assert "does not prove" in header["B2"].comment.text and "verified" not in header["B2"].comment.text.lower()
    assert fill(header["I2"]) == MARKS["filled"][0] and header["I2"].comment.text.startswith("AI filled")
    assert fill(header["G2"]) == MARKS["disputed"][0] and "2026-10-04" in header["G2"].comment.text and "2026-10-05" in header["G2"].comment.text
    assert fill(details["D2"]) == MARKS["disputed"][0] and "Local: 5. AI: 9." in details["D2"].comment.text
    assert fill(details["E2"]) == MARKS["confirmed"][0]
    assert header["A2"].comment.text.startswith("In dispute")
    assert book.sheetnames == ["Header", "Tax_Breakdown", "Details"], "no sheet or column added to the template"
    assert header["B2"].value == "A1" and details.max_column == 6


def test_the_excel_says_not_ai_confirmed_with_no_cell_marked(client, owner, app, tmp_path):
    invoice = {"number": "A1", "net": "5", "tax": "1", "date": "2026-10-04", "lines": [{"sku": "ABC-1", "qty": "1", "price": "5"}]}
    book = workbook_of(client, owner, app, tmp_path, {"ai_check": ai_check.not_read("failed")}, invoice)
    first = book["Header"]["A2"]
    assert fill(first) == MARKS["not_confirmed"][0]
    assert first.comment.text == "Not AI-confirmed: the AI read failed or timed out."
    assert book["Header"]["B2"].comment is None and book["Details"]["D2"].comment is None
    assert book["Header"]["B2"].fill.fill_type is None


def test_an_empty_cell_in_the_excel_is_not_marked_confirmed(client, owner, app, tmp_path):
    invoice = {"number": "A1", "net": "5", "tax": "1", "po": "P1", "date": "2026-10-04", "lines": [{"sku": "ABC-1", "qty": "1", "price": "5"}]}
    job_with(app.state.store, tmp_path, readers=record_for(invoice, invoice), invoice=invoice)
    view = client.get("/api/jobs/dis", headers=bearer(owner)).json()["rules"]
    assert view["ai_check"]["header"]["po"] == "confirmed"
    view["fields"]["po"].update(value=None, flagged=True, evidence=[])
    book = load_workbook(io.BytesIO(rules_workbook([view], "empty", blanks=True)[0]))
    assert book["Header"]["D2"].value is None and book["Header"]["D2"].comment is None


def test_a_settled_cell_in_the_excel_is_not_called_confirmed(client, owner, app, tmp_path):
    invoice = {"number": "A1", "net": "6", "tax": "1", "date": "2026-10-04", "lines": [{"sku": "ABC-1", "qty": "1", "price": "5"}]}
    readers = ai_check.settle(record_for({**invoice, "net": "5"}, {**invoice, "net": "6"}), {"net": "ai"}, invoice, "someone", "now")
    book = workbook_of(client, owner, app, tmp_path, readers, invoice)
    assert book["Header"]["H2"].comment.text.startswith("A person settled") and fill(book["Header"]["H2"]) == MARKS["settled"][0]


# Learning

def test_learning_leaves_out_a_value_only_the_ai_read():
    local = {"number": "A1", "po": None, "lines": [{"qty": "1", "uom": None}]}
    final = {"number": "A1", "po": "P1", "lines": [{"qty": "1", "uom": "PCS"}]}
    readers = {"ai_check": ai_check.compare(local, final, final, {"po": "ai"}, [{"uom": "ai"}])}
    learned = ai_check.learnable(final, readers)
    assert learned["number"] == "A1" and learned["po"] is None
    assert learned["lines"] == [{"qty": "1", "uom": None}]
    assert final["po"] == "P1", "the job keeps its value"
    typed = {**final, "po": "P2"}
    assert ai_check.learnable(typed, readers)["po"] == "P2", "a value a person typed is theirs"


def test_learning_keeps_a_value_a_person_settled():
    invoice = {"number": "A1", "net": "6"}
    readers = ai_check.settle(record_for({**invoice, "net": "5"}, invoice), {"net": "ai"}, invoice, "someone", "now")
    assert ai_check.learnable(invoice, readers)["net"] == "6"


def test_the_learning_route_teaches_without_the_ai_only_value(tmp_path, monkeypatch):
    from app import main
    taught = {}
    monkeypatch.setenv("INV_STUDIO_LEARN", "1")
    monkeypatch.setattr(main, "stored_learn", lambda text, boxes, tables, invoice, jid, root: taught.update(invoice) or {"writes": [], "outcome": {}})
    app = main.create_app(tmp_path / "data", store_type=Store)
    local = {"number": "A1", "net": "5", "tax": None, "lines": []}
    final = {**local, "tax": "1"}
    app.state.store.job("done", {"id": "done", "revision": 2, "status": "exported", "invoice": final,
                                 "readers": {"ai_check": ai_check.compare(local, final, final, {"tax": "ai"}, [])}})
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        assert c.post("/api/learned/jobs/done", headers=HEADERS).status_code == 200
    assert taught["number"] == "A1" and taught["tax"] is None


def pick(line=0):
    return {"line": line, "kind": "item", "field": "item_id", "value": "1001", "source": "Item master", "why": "same", "row_site": "NW1", "row_supplier": "Northwind"}


def test_a_rule_needs_both_reads_on_the_supplier_and_the_printed_code():
    local = {"supplier_name": "Northwind LLC", "lines": [{"sku": "NW-100"}, {"sku": "NW-200"}]}
    agreed = {"supplier_name": "NORTHWIND L.L.C.", "lines": [{"sku": "nw 100"}, {"sku": "NW-999"}]}
    assert ai_rules.both_read(local, agreed, [pick(0), pick(1)]) == [pick(0)]
    assert ai_rules.both_read(local, {**agreed, "supplier_name": "Other"}, [pick(0)]) == []
    assert ai_rules.both_read(local, {**agreed, "lines": agreed["lines"][:1]}, [pick(0)]) == []
    assert ai_rules.both_read(local, None, [pick(0)]) == []


def test_a_rule_keeps_the_close_names_check_after_both_reads_agree(store):
    local = {"supplier_name": "Northwind LLC", "lines": [{"sku": "NW-100"}]}
    both = ai_rules.both_read(local, local, [{**pick(), "row_supplier": "Northwind Trading"}])
    assert both and ai_rules.add(store, local, both, sites=[]) == []
    both = ai_rules.both_read(local, local, [pick()])
    assert len(ai_rules.add(store, local, both, sites=[])) == 1


# What it costs

def test_tokens_and_calls_are_counted_per_invoice(read):
    result, calls, _ = read(local=whole(), meta=USAGE, **FOUNDRY)
    assert calls == ["ai"]
    assert costs.usage_of(result["trace"]) == {"calls": 1, "input_tokens": 900, "output_tokens": 200, "cached_tokens": 100}
    summary = costs.summarise([{"day": "2026-11-06", "cost": costs.record(result["trace"], "m", "2026-11-06")},
                               {"day": "2026-11-06", "cost": {"calls": 0, "usd": "0"}}])
    assert summary["per_invoice"] == {"ai_calls": 0.5, "input_tokens": 450.0, "output_tokens": 100.0, "cached_tokens": 50.0}


def test_a_failed_read_costs_a_call_on_the_reader_and_no_tokens(read):
    result, _, _ = read(local=whole(), ai=RuntimeError("down"), **FOUNDRY)
    assert result["readers"]["ai"]["calls"] == 1 and costs.usage_of(result["trace"])["calls"] == 0


# What the reviewer of #53 found: agreement by a fold that lost separators and marks

@pytest.mark.parametrize("field,one,other", [
    ("number", "INV-12-3", "INV-1-23"), ("po", "12/34", "1/234"), ("number", "---", "///"),
    ("date_printed", "1/11/2026", "11/1/2026"), ("number", "SI/5.5", "SI/55"), ("number", "A½", "A12"),
    ("supplier_name", "कल", "काल"), ("number", "A-1", "A1"),
])
def test_values_that_differ_in_a_separator_or_a_mark_are_a_dispute(field, one, other):
    assert not ai_check.same(field, one, other)
    assert ai_check.view(record_for({field: one}, {field: other}), {field: one})["status"] == "disputed"


@pytest.mark.parametrize("one,other", [("SI/123", "SI-123"), ("SI--123", "si 123"), ("A-1.", "A-1"), ("شركة", "شَركة"), ("A‏1", "A1")])
def test_the_same_value_in_other_print_still_agrees(one, other):
    assert ai_check.same("number", one, other)


def test_a_sku_that_differs_in_a_separator_is_a_dispute():
    readers = record_for({"lines": [{"sku": "AB-1-2"}]}, {"lines": [{"sku": "AB-12"}]})
    assert ai_check.view(readers, {"lines": [{"sku": "AB-1-2"}]})["lines"] == [{"sku": "disputed"}]


# Retry

def test_a_read_that_says_nothing_keeps_an_open_dispute():
    old = record_for({"net": "5", "number": "A1"}, {"net": "6", "number": "A1"})["ai_check"]
    readers = {"ai_check": ai_check.carry(old, ai_check.not_read("failed"))}
    shown = ai_check.view(readers, {"net": "5", "number": "A1"})
    assert shown["status"] == "disputed" and shown["header"] == {"net": "disputed"} and [x["key"] for x in shown["disputes"]] == ["net"]
    assert shown["why"] == ai_check.NOT_READ["failed"] and ai_check.issues(shown)[0]["code"] == ai_check.ISSUE
    assert ai_check.carry(readers["ai_check"], ai_check.not_read("off"))["carried"], "a second empty read keeps it too"
    settled = ai_check.settle(readers, {"net": "local"}, {"net": "5"}, "someone", "now")
    assert ai_check.view(settled, {"net": "5"})["status"] == "not_confirmed", "a person can still settle it, and nothing was read"


@pytest.mark.parametrize("new,left", [({"net": "5"}, []), ({"net": "7"}, []), ({"net": None}, ["net"]), ({"number": "A1"}, ["net"])])
def test_only_a_new_read_that_compared_the_cell_answers_a_dispute(new, left):
    old = record_for({"net": "5"}, {"net": "6"})["ai_check"]
    fresh = record_for({"net": "5"}, new)["ai_check"]
    assert [x["key"] for x in ai_check.carry(old, fresh).get("carried", [])] == left
    assert [x["key"] for x in ai_check.disputes(ai_check.carry(old, fresh))] == (["net"] if new.get("net") == "7" else left)


def test_a_settled_dispute_is_not_carried():
    readers = ai_check.settle(record_for({"net": "5"}, {"net": "6"}), {"net": "local"}, {"net": "5"}, "someone", "now")
    assert "carried" not in ai_check.carry(readers["ai_check"], ai_check.not_read("failed"))


@pytest.mark.parametrize("new,left", [({"lines": [{"qty": "1"}, {"qty": "2"}]}, []), ({"lines": []}, ["lines"])])
def test_a_line_count_dispute_needs_rows_compared_to_end(new, left):
    rows = [{"qty": "1"}, {"qty": "2"}]
    old = record_for({"lines": rows}, {"lines": rows[:1]})["ai_check"]
    fresh = record_for({"lines": rows}, new)["ai_check"]
    assert [x["key"] for x in ai_check.carry(old, fresh).get("carried", [])] == left


def test_a_retry_with_the_ai_off_does_not_open_the_export(tmp_path, monkeypatch):
    from tests.test_batch import OPTIONS, FakeReader, finished, plan, studio, upload
    app, client, token = studio(tmp_path, monkeypatch, FakeReader())
    job = upload(client, token, plan(client, token, ["a.txt"], OPTIONS), "a.txt", OPTIONS).json()
    assert finished(app, [job["id"]])
    stored = app.state.store.job(job["id"])
    stored["readers"] = {**stored["readers"], "ai_check": ai_check.compare({"net": "5"}, {"net": "6"}, {"net": "5"}, {}, [])}
    app.state.store.job(job["id"], stored)
    retry = client.post(f"/api/jobs/{job['id']}/retry", json={"options": OPTIONS, "preflight_token": plan(client, token, ["a.txt"], OPTIONS)}, headers=bearer(token))
    assert retry.status_code == 200, retry.text
    assert finished(app, [job["id"]])
    after = client.get(f"/api/jobs/{job['id']}", headers=bearer(token)).json()
    assert after["rules"]["ai_check"]["status"] == "disputed" and [x["key"] for x in after["rules"]["ai_check"]["disputes"]] == ["net"]
    refused = client.post(f"/api/jobs/{job['id']}/export", headers=bearer(token), json={"revision": after["revision"]})
    assert refused.status_code == 409 and "differ" in refused.json()["detail"]


# The target workbook

@pytest.fixture
def approved(monkeypatch):
    """Rules that approve everything with evidence, so only the AI check is left to stop the workbook."""
    from app import main
    real = main.rules_view

    def approving(*args, **kwargs):
        view = real(*args, **kwargs)
        for cell in [*view["fields"].values(), *(c for line in view["lines"] for c in line["cells"].values())]:
            cell.update(value=cell["value"] or "5", flagged=False, reason="", evidence=[{"kind": "document"}])
        view["fields"]["date"]["value"] = "2026-10-04"
        view["fields"]["location_type"]["value"] = STORE
        return {**view, "status": "Approved", "issues": []}
    monkeypatch.setattr(main, "rules_view", approving)


def background(client, owner, kind, body):
    return client.post(f"/api/background/{kind}", headers=bearer(owner), json=body)


def test_the_final_template_is_refused_like_the_export_while_a_dispute_is_open(client, owner, app, tmp_path, approved):
    invoice = job_with(app.state.store, tmp_path)
    job = client.get("/api/jobs/dis", headers=bearer(owner)).json()
    assert job["rules"]["status"] == "Review", "not Approved with a dispute open"
    refused = client.post("/api/fine-rules/target.xlsx", headers=bearer(owner), json={"job_ids": ["dis"]})
    assert refused.status_code == 409 and "differ" in refused.json()["detail"]
    started = background(client, owner, "fine-rules-target", {"job_ids": ["dis"]})
    assert started.status_code == 409 and "differ" in started.json()["detail"]
    settled = review(client, owner, "dis", {**invoice, "net": "6"}, 1, settled={"net": "ai"}).json()
    assert settled["rules"]["status"] == "Approved" and settled["rules"]["ai_check"]["disputes"] == []
    allowed = client.post("/api/fine-rules/target.xlsx", headers=bearer(owner), json={"job_ids": ["dis"]})
    assert allowed.status_code == 200, allowed.text


def test_the_final_template_is_refused_for_a_duplicate_like_the_export(client, owner, app, tmp_path, approved):
    assert agreed_export(client, owner, app, tmp_path, "one", "501").status_code == 200
    assert agreed_export(client, owner, app, tmp_path, "two", "501").status_code == 409
    assert client.post("/api/fine-rules/target.xlsx", headers=bearer(owner), json={"job_ids": ["two"]}).status_code == 409


# A mark belongs to the value in the cell

def test_a_typed_value_gets_no_ai_mark_on_screen_or_in_the_excel(client, owner, app, tmp_path):
    invoice = {"number": "A1", "net": "5", "tax": "1", "po": "LPO-9", "date": "2026-10-04", "lines": [{"sku": "ABC-1", "qty": "1", "price": "5"}]}
    job_with(app.state.store, tmp_path, readers=record_for(invoice, invoice), invoice=invoice)
    job = review(client, owner, "dis", invoice, 1, entries={"header": {"net": "777", "tax": "1", "po": "999999"}}).json()
    check = job["rules"]["ai_check"]
    assert check["header"]["net"] == "confirmed", "the invoice's own net still agrees"
    assert not {"net", "tax", "po"} & set(check["written"]["header"]), "typed, even when equal to what the reads saw"
    book = load_workbook(io.BytesIO(rules_workbook([job["rules"]], "empty", blanks=True)[0]))
    assert book["Header"]["H2"].value == 777 and book["Header"]["H2"].comment is None and book["Header"]["H2"].fill.fill_type is None
    assert book["Header"]["I2"].comment is None and book["Header"]["D2"].comment is None and book["Tax_Breakdown"]["C2"].comment is None


def rules_with(**values):
    return {"fields": {k: {"value": v, "evidence": [{"kind": "document"}]} for k, v in values.items()}, "lines": []}


def test_a_target_value_other_than_the_compared_one_gets_no_mark():
    invoice = {"number": "A1", "po": "LPO-9", "net": "5"}
    readers = record_for(invoice, invoice)
    check = ai_check.view(readers, invoice)
    assert ai_check.written(readers, check, rules_with(number="A1", po="RF-1", net="5.00"))["header"] == {"number": "confirmed", "net": "confirmed"}, "a derived Order No"


def test_a_target_line_cell_shows_only_its_own_compared_value():
    invoice = {"lines": [{"sku": "ABC", "gtin": "6291000000001", "qty": "2", "price": "5"}]}
    readers = record_for(invoice, invoice)
    cell = {"evidence": [{"kind": "document"}]}
    rules = {"fields": {}, "lines": [{"line": 1, "cells": {"Item": {**cell, "value": "1001"}, "UPC": {**cell, "value": "6291000000001"},
                                                           "Unit Cost": {**cell, "value": "5.00"}, "Quantity": {**cell, "value": "3"}}}]}
    assert ai_check.written(readers, ai_check.view(readers, invoice), rules)["lines"] == [{"UPC": "confirmed", "Unit Cost": "confirmed"}]


def test_a_dispute_stays_on_a_derived_target_cell_and_an_agreement_does_not():
    local = {"supplier_name": "One", "buyer_name": "Two"}
    readers = record_for(local, {"supplier_name": "Other", "buyer_name": "Two"})
    shown = ai_check.written(readers, ai_check.view(readers, local), rules_with(site="501", location="9"))
    assert shown["header"] == {"site": "disputed"}


def test_the_screen_marks_target_cells_from_the_written_states():
    script = (ai_check.__file__.rsplit("/", 1)[0] + "/static/app.js")
    with open(script, encoding="utf-8") as source:
        text = source.read()
    assert "written?.header?.[column]" in text and "written?.lines?.[line - 1]?.[column]" in text
    assert 'readerNotes(job, "header", key, undefined, enteredByReviewer(field))' in text


# Settled shows only while it holds

def test_settled_stops_showing_when_the_value_is_edited():
    invoice = {"net": "6"}
    readers = ai_check.settle(record_for({"net": "5"}, invoice), {"net": "ai"}, invoice, "someone", "now")
    assert ai_check.view(readers, {"net": "6.00"})["header"] == {"net": "settled"}
    shown = ai_check.view(readers, {"net": "999"})
    assert shown["header"] == {} and shown["disputes"] == [] and shown["counts"]["settled"] == 0


def test_a_settled_line_mark_does_not_land_on_another_row():
    rows = [{"sku": "A", "qty": "1"}, {"sku": "B", "qty": "2"}]
    readers = record_for({"lines": rows}, {"lines": [rows[0], {"sku": "B", "qty": "3"}]})
    readers = ai_check.settle(readers, {"line 2 qty": "local"}, {"lines": rows}, "someone", "now")
    assert ai_check.view(readers, {"lines": rows})["lines"][1]["qty"] == "settled"
    assert "qty" not in ai_check.view(readers, {"lines": [rows[0], {"sku": "C", "qty": "8"}]})["lines"][1]
    assert "qty" not in ai_check.view(readers, {"lines": rows[1:]})["lines"][1], "nor after a row went"


# A wholly different description

@pytest.mark.parametrize("one,other", [("Rose hand cream", "Totally different product"), ("كريم اليدين", "Blue shampoo"), ("Soap and water", "Nails and screws")])
def test_a_wholly_different_description_is_a_dispute(one, other):
    readers = record_for({"lines": [{"description": one}]}, {"lines": [{"description": other}]})
    assert [x["key"] for x in ai_check.view(readers, {"lines": [{"description": one}]})["disputes"]] == ["line 1 description"]


@pytest.mark.parametrize("one,other", [
    ("Serum 30ml", "Serum 30 ml box"), ("ROSE HAND CREAM", "R0SE HAND CREAM"), ("Shampoo", "Shamp00"), ("Rosehandcream", "Rose hand cream"),
    ("Moisturiser", "Moisturizer"), ("30 ml", "Totally different"), ("AB 12", "CD 34"),
])
def test_ordinary_differences_in_a_description_stay_quiet(one, other):
    readers = record_for({"lines": [{"description": one}]}, {"lines": [{"description": other}]})
    assert ai_check.view(readers, {"lines": [{"description": one}]})["disputes"] == []


# A lines pick

def test_a_lines_pick_keeps_every_field_of_the_row():
    full = {"item_id": "1001", "part_code": "P-7", "evidence": "the row", "page": 2, "sku": "A", "qty": "1", "barcode_unchecked": "123"}
    readers = record_for({"lines": [full]}, {"lines": [{"sku": "A", "qty": "1"}, {"sku": "B", "qty": "1"}]})
    dispute = ai_check.view(readers, {"lines": [full]})["disputes"][0]
    assert dispute["key"] == "lines" and dispute["local_lines"] == [full]


# Learning, once the rows moved

@pytest.mark.parametrize("rows", [
    lambda line: [line],  # line 2 removed
    lambda line: [line, {"qty": "2"}, {"qty": "3"}],  # a row added
    lambda line: [{"qty": "2"}, line],  # moved
])
def test_an_ai_fill_does_not_teach_when_the_rows_changed(rows):
    local = {"lines": [{"qty": "1", "uom": None, "gtin": None}, {"qty": "2"}]}
    filled = {"qty": "1", "uom": "CTN", "gtin": "6291000000001"}
    final = {"lines": [filled, {"qty": "2"}]}
    readers = {"ai_check": ai_check.compare(local, final, final, {}, [{"uom": "ai", "gtin": "ai"}, {}])}
    assert not any(row.get("uom") or row.get("gtin") for row in ai_check.learnable(final, readers)["lines"])
    taught = ai_check.learnable({"lines": rows(filled)}, readers)["lines"]
    assert not any(row.get("uom") or row.get("gtin") for row in taught)


def test_learning_keeps_a_cell_both_reads_agree_on_beside_the_same_ai_fill():
    local = {"lines": [{"qty": "1", "uom": None}, {"qty": "2", "uom": "CTN"}]}
    final = {"lines": [{"qty": "1", "uom": "CTN"}, {"qty": "2", "uom": "CTN"}]}
    readers = {"ai_check": ai_check.compare(local, final, final, {}, [{"uom": "ai"}, {}])}
    assert [row["uom"] for row in ai_check.learnable(final, readers)["lines"]] == [None, "CTN"]
