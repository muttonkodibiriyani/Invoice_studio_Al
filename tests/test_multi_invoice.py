"""A PDF that binds several invoices is split by page into one job per invoice, or refused naming every number."""

import io
import json
import time

import pytest

from app import engines, multi_invoice
from app.multi_invoice import analyse_pages, analyse_pdf, outcome_from, part_filename, pdf_pages, split_pdf
from tests.conftest import audit_rows, bearer
from tests.test_batch import studio

OPTIONS = {"engine": "auto", "ai_fallback": False, "provider": "openai", "model": "", "language": "en"}


def _bound_pdf():
    """Two synthetic one-page tax invoices bound in one PDF: one item line each, with its own number and totals.

    Rows are (top, [(x, text), ...]) in 7 pt Helvetica, the layout of the dropped fixture two-invoices-synthetic.pdf."""
    from reportlab.pdfgen import canvas
    columns = (34, 56, 181, 233, 278, 331, 380, 447, 476, 534)
    heading = ("#", "Description", "Brand", "Quantity", "UOM", "Price (Excl. VAT),", "Amount (Excl. VAT),", "VAT,",
               "VAT Amount,", "Amount (Incl. VAT),")
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(595, 842))
    for store, number, item, quantity, price, net, tax, gross in (
            (1, "ZZTI26-00000101", "Hydra Gel Cream 50ml,", "3.000", "12.50", "37.50", "1.88", "39.38"),
            (2, "ZZTI26-00000102", "Lip Tint Coral LTC-04,", "4.000", "6.00", "24.00", "1.20", "25.20")):
        rows = [(44, [(220, "TAX INVOICE")]), (55, [(31, f"# {number}")]),
                (66, [(31, "Date of Issuing: March 7, 2026")]), (77, [(31, "Date of Supply: March 7, 2026")]),
                (88, [(31, "Issued By:"), (300, "Issued To:")]),
                (99, [(31, "Example Trading LLC"), (300, f"Sample Retail Co - Store {store}")]),
                (110, [(31, "Reference #: 13000042"), (200, "Reference Date:")]),
                (121, list(zip(columns, heading))), (132, [(331, "XYZ"), (380, "XYZ"), (476, "XYZ"), (534, "XYZ")]),
                (143, list(zip(columns, ("1", item, "NORTHLEAF", quantity, "Pcs", price, net, "5", tax, gross)))),
                (154, [(379, f"Sub Total, XYZ: {net}")]), (165, [(379, f"Total VAT, XYZ: {tax}")]),
                (176, [(379, f"Total, XYZ: {gross}")]), (187, [(30, "Terms and Conditions:")]), (198, [(500, "Page 1 of 1")])]
        page.setFont("Helvetica", 7)
        for top, cells in rows:
            for x, text in cells:
                page.drawString(x, 842 - top - 6, text)
        page.showPage()
    page.save()
    return buffer.getvalue()


BOUND = _bound_pdf()


def test_two_numbers_on_separate_pages_split_by_page():
    outcome = analyse_pages(["TAX INVOICE\n# ZZTI26-00000101\nPage 1 of 2", "continued\nPage 2 of 2",
                             "TAX INVOICE\n# ZZTI26-00000102\nPage 1 of 1"])
    assert outcome.multiple and outcome.refusal is None
    assert [(s.number, s.pages) for s in outcome.segments] == [("ZZTI26-00000101", [1, 2]), ("ZZTI26-00000102", [3])]


def test_page_counter_restart_starts_a_new_invoice_before_its_number_is_read():
    outcome = analyse_pages(["Tax Invoice 1000000001\nPage 1 of 1", "Page 1 of 2\nTax Invoice 1000000002", "Page 2 of 2"])
    assert [(s.number, s.pages) for s in outcome.segments] == [("1000000001", [1]), ("1000000002", [2, 3])]


def test_one_invoice_over_several_pages_is_not_split():
    outcome = analyse_pages(["Tax Invoice 1000000001\nPage 1 of 2", "Tax Invoice 1000000001\nPage 2 of 2"])
    assert not outcome.multiple and outcome.refusal is None
    assert [(s.number, s.pages) for s in outcome.segments] == [("1000000001", [1, 2])]


def test_numbers_in_the_body_and_dates_are_not_invoice_numbers():
    body = "Tax Invoice 1000000001\n" + "\n".join(f"line {n}" for n in range(40)) + "\nreplaces invoice 1000000000"
    outcome = analyse_pages([body, "Invoice Date 12/03/2026\nInvoice No. Date"])
    assert outcome.numbers == ["1000000001"]


@pytest.mark.parametrize("pages", [
    ["Tax Invoice 1000000001 Tax Invoice 1000000002"],
    ["Tax Invoice 1000000001", "Tax Invoice 1000000002", "Tax Invoice 1000000001"],
])
def test_mixed_pages_are_refused_naming_both_numbers(pages):
    outcome = analyse_pages(pages)
    assert outcome.multiple and outcome.segments == []
    assert "1000000001" in outcome.refusal and "1000000002" in outcome.refusal and "Split the PDF" in outcome.refusal


def test_scan_without_text_is_left_to_the_readers():
    assert not analyse_pages(["", ""]).multiple


def _in_reader_child(content, root):
    """analyse_pdf as the upload runs it since #78: in a reader child, its answer checked by outcome_from."""
    (root / "work").mkdir(exist_ok=True)
    return outcome_from(engines.split_check(content, root))


def test_bound_pdf_splits_into_two_readable_pdfs(tmp_path):
    content = BOUND
    outcome = analyse_pdf(content)
    assert _in_reader_child(content, tmp_path) == outcome
    parts = split_pdf(content, outcome.segments)
    assert outcome.numbers == ["ZZTI26-00000101", "ZZTI26-00000102"] and len(parts) == 2
    for part, number in zip(parts, outcome.numbers):
        assert part.startswith(b"%PDF-") and analyse_pages(_texts(part)).numbers == [number]
    assert part_filename("bound invoices.pdf", 2, 2, "ZZTI26-00000102") == "bound invoices [2 of 2 ZZTI26-00000102].pdf"


def _column_heading_pdf(numbers):
    """Synthetic layout that prints the number under an 'Invoice Number' column heading, with a label row between."""
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(595, 842))
    for number in numbers:
        page.drawString(40, 790, "Invoice Number"); page.drawString(200, 790, "Invoice Date"); page.drawString(340, 790, "Customer")
        page.drawString(40, 776, "Synthetic label row"); page.drawString(200, 776, "Synthetic label")
        page.drawString(40, 762, number); page.drawString(200, 762, "01/02/2026"); page.drawString(340, 762, "SYNTHETIC BUYER")
        page.drawString(40, 300, "Item 777700001111 Qty 2"); page.showPage()
    page.save()
    return buffer.getvalue()


def test_number_under_an_invoice_number_column_heading_splits_the_pdf(tmp_path):
    content = _column_heading_pdf(["900000000101", "900000000102"])
    texts, columns = pdf_pages(content)
    assert analyse_pages(texts).numbers == []  # no number beside a label: the column heading is what finds them
    assert columns == [["900000000101"], ["900000000102"]]
    outcome = analyse_pdf(content)
    assert outcome.multiple and outcome.numbers == ["900000000101", "900000000102"]
    assert [segment.pages for segment in outcome.segments] == [[1], [2]]
    assert _in_reader_child(content, tmp_path) == outcome
    assert analyse_pdf(_column_heading_pdf(["900000000101"])).multiple is False


def _texts(content):
    import pdfplumber
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def _studio(tmp_path, monkeypatch, reader=engines.process_job):
    """The app signed in as the bootstrap administrator, with the real readers unless the test wraps them.

    The split check must run in the reader child (#78): pdf_pages fails the test if the API process calls it."""
    def in_process(*args, **kwargs):
        raise AssertionError("the split check ran inside the API process")
    monkeypatch.setattr(multi_invoice, "pdf_pages", in_process)
    app, client, token = studio(tmp_path, monkeypatch, reader)
    client.headers.update(bearer(token))
    return app, client


def _upload(client, name, content):
    plan = client.post("/api/preflight", json={"options": OPTIONS, "files": [{"name": name, "size": len(content)}]}).json()
    client.post("/api/preflight/confirm", json={"token": plan["token"]}).raise_for_status()
    response = client.post("/api/invoices", files={"file": (name, content, "application/pdf")},
                           data={"options": json.dumps(OPTIONS), "preflight_token": plan["token"]})
    assert response.status_code == 200, response.text
    return response.json()


def _settled(client, jid):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{jid}").json()
        if job["status"] not in ("queued", "processing"):
            return job
        time.sleep(0.05)
    pytest.fail("extraction did not finish")


def test_upload_of_bound_pdf_creates_one_job_per_invoice(tmp_path, monkeypatch):
    app, client = _studio(tmp_path, monkeypatch)
    with client:
        first = _upload(client, "bound invoices.pdf", BOUND)
        jobs = [j for j in client.get("/api/state").json()["jobs"] if j.get("split")]
        assert len(jobs) == 2 and first["id"] in {j["id"] for j in jobs}
        by_part = {j["split"]["part"]: _settled(client, j["id"]) for j in jobs}
        for part, number, net, tax in ((1, "ZZTI26-00000101", "37.50", "1.88"), (2, "ZZTI26-00000102", "24.00", "1.20")):
            job = by_part[part]
            assert job["status"] == "review", job.get("error")
            assert job["split"]["of"] == 2 and job["split"]["pages"] == [part] and job["split"]["source_filename"] == "bound invoices.pdf"
            assert job["filename"] == f"bound invoices [{part} of 2 {number}].pdf"
            assert job["invoice"]["number"] == number
            assert (job["invoice"]["net"], job["invoice"]["tax"]) == (net, tax)
            assert len(job["invoice"]["lines"]) == 1
            assert "ZZTI26-00000101, ZZTI26-00000102" in job["extraction_note"] and f"invoice {part} of 2" in job["extraction_note"]
            assert "path" not in job
    uploaded = [json.loads(payload) for event, payload in audit_rows(app) if event == "uploaded"]
    assert len(uploaded) == 2 and all("invoice_number" not in row["split"] and "note" not in row["split"] for row in uploaded)


def test_upload_of_mixed_page_pdf_is_refused_with_both_numbers(tmp_path, monkeypatch):
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(595, 842))
    page.drawString(40, 800, "TAX INVOICE # ZZTI26-00000201")
    page.drawString(40, 400, "TAX INVOICE # ZZTI26-00000202")
    page.showPage(); page.save()
    _, client = _studio(tmp_path, monkeypatch)
    with client:
        job = _upload(client, "mixed.pdf", buffer.getvalue())
        assert job["status"] == "error" and job["extraction_status"] == "multi_invoice_refused"
        assert "ZZTI26-00000201" in job["error"] and "ZZTI26-00000202" in job["error"]
        assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "error"
        assert len(client.get("/api/state").json()["jobs"]) == 1
        # The queue slot is released: another upload still runs.
        again = _upload(client, "single.pdf", _single_pdf())
        assert _settled(client, again["id"])["status"] == "review"


def _single_pdf():
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(595, 842))
    page.drawString(40, 800, "TAX INVOICE # ZZTI26-00000301"); page.showPage(); page.save()
    return buffer.getvalue()


def test_a_split_part_read_again_keeps_one_split_note(tmp_path, monkeypatch):
    def noted(*args, **kwargs):
        result = engines.process_job(*args, **kwargs)
        return {**result, "extraction_note": "Synthetic reader note."}
    _, client = _studio(tmp_path, monkeypatch, noted)
    with client:
        _upload(client, "bound invoices.pdf", BOUND)
        job = next(j for j in client.get("/api/state").json()["jobs"] if (j.get("split") or {}).get("part") == 1)
        before = _settled(client, job["id"])["extraction_note"]
        for _ in range(2):
            plan = client.post("/api/preflight", json={"options": OPTIONS, "files": [{"name": job["filename"], "size": job["size"]}]}).json()
            client.post("/api/preflight/confirm", json={"token": plan["token"]}).raise_for_status()
            client.post(f"/api/jobs/{job['id']}/retry", json={"options": OPTIONS, "preflight_token": plan["token"]}).raise_for_status()
            after = _settled(client, job["id"])["extraction_note"]
            assert after == before and after.count("It was split by page") == 1
            assert after.count("Synthetic reader note.") == 1
