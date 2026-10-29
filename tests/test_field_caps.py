"""RULE 8: every free-text field of an invoice and its lines has a maximum length, enforced by the model.

A read (local reader, AI, a stored invoice) keeps an over-cap field empty, so it shows as a gap; a member's
saved edit over a cap is refused with a 422 and changes nothing. The checks after a read run patterns over
these values, so the cap bounds their input.
"""
import copy
import json
import typing

import pytest

from app.models import EditedInvoice, EditedLine, Invoice, Line, _max_length, parse_ai_output
from tests.conftest import bearer
from tests.test_reader_chain import INVOICE, chain  # noqa: F401  (the stub reader chain fixture)


def text_fields(model):
    return [name for name, field in model.model_fields.items() if str in typing.get_args(field.annotation)]


def over(model, name):
    return "x" * (_max_length(model.model_fields[name]) + 1)


@pytest.mark.parametrize("model", [Invoice, Line, EditedInvoice, EditedLine])
def test_every_free_text_field_has_a_cap(model):
    assert text_fields(model), model
    assert all(_max_length(model.model_fields[name]) < float("inf") for name in text_fields(model)), model


def test_a_read_keeps_an_over_cap_field_empty_and_every_other_value():
    data = {**{name: over(Invoice, name) for name in text_fields(Invoice)}, "net": "10.00", "po": "PO-1",
            "lines": [{**{name: over(Line, name) for name in text_fields(Line)}, "qty": "2", "sku": "A1"}]}
    invoice = Invoice.model_validate(data)
    assert {name: getattr(invoice, name) for name in text_fields(Invoice)} == {
        name: ("PO-1" if name == "po" else None) for name in text_fields(Invoice)}
    assert str(invoice.net) == "10.00"
    line = invoice.lines[0]
    assert {name: getattr(line, name) for name in text_fields(Line)} == {
        name: ("A1" if name == "sku" else None) for name in text_fields(Line)}
    # A value at the cap is kept whole; surrounding whitespace does not count against it.
    at_cap = "y" * _max_length(Invoice.model_fields["number"])
    assert Invoice.model_validate({"number": f"  {at_cap} "}).number == at_cap
    assert Invoice.model_validate_json(json.dumps({"number": over(Invoice, "number")})).number is None


def test_local_reader_output_over_a_cap_reaches_the_job_empty(chain):  # noqa: F811
    read = copy.deepcopy(INVOICE)
    read["number"] = over(Invoice, "number")
    read["lines"][0]["description"] = over(Line, "description")
    result, _ = chain("invoice.pdf", reads={"paddleocr": read}, ai_fallback=False)
    assert result["selected_engine"] == "paddleocr"
    assert result["invoice"]["number"] is None and result["invoice"]["lines"][0]["description"] is None
    assert result["invoice"]["supplier_name"] == INVOICE["supplier_name"]
    stored = json.dumps({k: v for k, v in result.items() if k != "text"}, default=str)
    assert read["number"] not in stored and read["lines"][0]["description"] not in stored


def test_ai_output_over_a_cap_is_empty_after_parsing():
    data = copy.deepcopy(INVOICE)
    data["supplier_name"] = over(Invoice, "supplier_name")
    data["lines"][0]["sku"] = over(Line, "sku")
    invoice, _ = parse_ai_output(json.dumps(data))
    assert invoice.supplier_name is None and invoice.lines[0].sku is None
    assert invoice.number == INVOICE["number"] and invoice.lines[0].description == INVOICE["lines"][0]["description"]


@pytest.fixture
def job(app):
    app.state.store.job("cap-job", {"id": "cap-job", "filename": "synthetic.txt", "status": "review", "revision": 1,
                                    "reviewed": False, "provenance": [], "trace": [],
                                    "invoice": Invoice.model_validate(INVOICE).model_dump(mode="json")})
    return app.state.store


def save(client, owner, invoice):
    return client.post("/api/jobs/cap-job/review", headers=bearer(owner), json={"invoice": invoice, "revision": 1})


@pytest.mark.parametrize("where", ["header", "line"])
def test_a_member_edit_over_a_cap_is_a_422_and_changes_nothing(client, owner, job, where):
    edited = copy.deepcopy(INVOICE)
    if where == "header":
        edited["number"] = over(Invoice, "number");field = "body.invoice.number"
    else:
        edited["lines"][0]["description"] = over(Line, "description");field = "body.invoice.lines.0.description"
    response = save(client, owner, edited)
    assert response.status_code == 422
    assert response.json()["detail"].startswith(f"Invalid input: {field}: String should have at most ")
    assert "xxxx" not in response.json()["detail"]  # the submitted value is not echoed
    stored = job.job("cap-job")
    assert stored["revision"] == 1 and stored["invoice"]["number"] == INVOICE["number"]


def test_a_member_edit_at_the_cap_is_saved(client, owner, job):
    edited = copy.deepcopy(INVOICE)
    edited["number"] = "N" * _max_length(Invoice.model_fields["number"])
    response = save(client, owner, edited)
    assert response.status_code == 200, response.text
    assert job.job("cap-job")["invoice"]["number"] == edited["number"]


def test_a_reviewer_entry_is_length_checked_before_any_format_pattern():
    from app.matching import _ENTRY_FORMAT, REQUIRED_HEADER, owner_entries
    name = next(k for k, kind in _ENTRY_FORMAT.items() if kind == "id")
    entry = {"header": {name: "1" * 65}} if name in REQUIRED_HEADER else {"lines": {"1": {name: "1" * 65}}}
    with pytest.raises(ValueError, match="too long"):
        owner_entries(entry)
