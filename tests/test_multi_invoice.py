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


INVOICES = ((1, "ZZTI26-00000101", "Hydra Gel Cream 50ml,", "3.000", "12.50", "37.50", "1.88", "39.38"),
            (2, "ZZTI26-00000102", "Lip Tint Coral LTC-04,", "4.000", "6.00", "24.00", "1.20", "25.20"))


def _bound_pdf(invoices=INVOICES):
    """Synthetic one-page tax invoices bound in one PDF (two by default): one item line each, with its own number and totals.

    Rows are (top, [(x, text), ...]) in 7 pt Helvetica, the layout of the dropped fixture two-invoices-synthetic.pdf."""
    from reportlab.pdfgen import canvas
    columns = (34, 56, 181, 233, 278, 331, 380, 447, 476, 534)
    heading = ("#", "Description", "Brand", "Quantity", "UOM", "Price (Excl. VAT),", "Amount (Excl. VAT),", "VAT,",
               "VAT Amount,", "Amount (Incl. VAT),")
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(595, 842))
    for store, number, item, quantity, price, net, tax, gross in invoices:
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
    texts, columns, _ = pdf_pages(content)
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
        # The queue slot is released: another upload still runs. A full invoice page, not one line: a PDF with almost
        # no text goes on to PaddleOCR where it is installed, over a minute on a loaded host.
        again = _upload(client, "single.pdf", _bound_pdf(INVOICES[:1]))
        assert _settled(client, again["id"])["status"] == "review"


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


# Made-up pages. No invoice number under a label the number patterns know: splitting must not need one.
def _page(supplier="Northwind Trading LLC", tax="100200300400500", title="Tax Invoice", ref="Doc Ref X-1001", date="01/03/2026",
          buyer="Sample Retail Co", rows=("A-1 Widget 3 10.00 30.00", "B-2 Gadget 2 15.00 30.00"), foot=("Total Payable 60.00",)):
    return "\n".join([supplier, "P.O. Box 1", f"TRN {tax}", title, ref, f"Date {date}", f"Customer {buyer}",
                      "Code Description Qty Price Amount", *rows, *foot])


def _rows(n=2):
    return tuple(f"R-{i} Item {i} 1 {i}0.00 {i}0.00" for i in range(1, n + 1))


@pytest.mark.parametrize("second", [
    _page(supplier="Alcora Packaging FZE", tax="900800700600500", title="Bill", ref="Voucher 77", date="02/04/2026", buyer="Other Co"),
    _page(ref="Doc Ref X-1002", date="02/03/2026"),  # same supplier: the header changed and the page before closed
    _page(ref="Doc Ref X-1002"),  # same supplier, same day
])
def test_invoices_with_no_readable_number_split_by_page(second):
    outcome = analyse_pages([_page(), second])
    assert outcome.numbers == [] and outcome.hold_pages == [] and outcome.refusal is None
    assert [s.pages for s in outcome.segments] == [[1], [2]]


def test_a_new_supplier_without_a_total_before_it_still_splits():
    outcome = analyse_pages([_page(foot=()), _page(supplier="Alcora Packaging FZE", tax="900800700600500", ref="Voucher 77")])
    assert [s.pages for s in outcome.segments] == [[1], [2]]


NATIVE_THEN_CONTINUES = [
    # The header repeats on every page, the total only on the last.
    [_page(foot=()), _page(rows=_rows(3))],
    # A short running head with the title first, then rows: nothing says a new invoice or a new supplier.
    [_page(foot=()), "\n".join(("Tax Invoice 01/03/2026", *_rows(8), "Total Payable 90.00"))],
    # Rows only, then the totals alone on the last page.
    [_page(foot=()), "\n".join(_rows(4)), "Sub Total 100.00\nVAT 5.00\nTotal 105.00"],
    # Carried and brought forward, with a total-like line on each page.
    [_page(foot=("Carried Forward 60.00",)), "\n".join(("Brought Forward 60.00", *_rows(3), "Total 120.00"))],
    # A printed counter that runs on outweighs a total on each page.
    [_page(foot=("Total 60.00", "Page 1 of 2")), _page(rows=_rows(3), foot=("Total 120.00", "Page 2 of 2"))],
    # A per-page subtotal.
    [_page(foot=("Page Total 60.00", "Sub Total 60.00")), _page(rows=_rows(3))],
    # Terms and bank details after the total: no amounts, nothing to lose.
    [_page(), "Tax Invoice\nDate 01/03/2026\nTerms and conditions\nPayment within 30 days.\nBank: Example Bank"],
    # The same totals box prints on every page.
    [_page(), _page(rows=_rows(3))],
    # The same header and total repeated with a 'continued' mark.
    [_page(), "Continued\n" + _page(rows=_rows(3))],
    # A trailing blank page.
    [_page(), ""],
    # A leading blank page.
    ["", _page()],
    # A barcode on a row beside a word that reads like a tax label.
    [_page(foot=()), "\n".join(("2995266784185 Olive oil 1L tin 3 10.00 30.00", *_rows(3), "Total 90.00"))],
]


@pytest.mark.parametrize("pages", NATIVE_THEN_CONTINUES)
def test_one_invoice_over_several_pages_neither_splits_nor_holds(pages):
    outcome = analyse_pages(pages)
    assert outcome.hold_pages == [] and outcome.refusal is None
    assert [s.pages for s in outcome.segments] == [list(range(1, len(pages) + 1))]


@pytest.mark.parametrize("pages,where", [
    # A title and a new date, but no total before it and the same supplier: only one signal.
    ([_page(foot=()), _page(date="09/09/2026", foot=())], [2]),
    # Two different grand totals with no header on the second page.
    ([_page(), "\n".join((*_rows(3), "Total Payable 75.00"))], [2]),
    # Another supplier's tax number, but the page does not open like an invoice.
    ([_page(), "\n".join(("TRN 900800700600500", *_rows(3)))], [2]),
    # Printed counters that neither run on nor restart.
    ([_page(foot=("Page 1 of 2",)), _page(rows=_rows(3), foot=("Page 4 of 5",))], [2]),
    # A whole new header with the same supplier and date, and nothing closed before it.
    ([_page(foot=()), _page(ref="Doc Ref X-1002", foot=())], [2]),
    # A printed number changes while the counter runs on: they disagree.
    (["Tax Invoice 1000000001\nPage 1 of 2", "Tax Invoice 1000000002\nPage 2 of 2"], [2]),
])
def test_unsure_pages_keep_the_file_whole_with_a_hold(pages, where):
    outcome = analyse_pages(pages)
    assert outcome.hold_pages == where and len(outcome.segments) == 1 and outcome.refusal is None
    assert outcome.segments[0].pages == list(range(1, len(pages) + 1))


def test_one_unsure_page_keeps_a_sure_split_whole_too():
    outcome = analyse_pages([_page(), _page(supplier="Alcora Packaging FZE", tax="900800700600500", ref="Voucher 77"),
                             "\n".join((*_rows(3), "Total Payable 75.00"))])
    assert outcome.hold_pages == [2, 3] and [s.pages for s in outcome.segments] == [[1, 2, 3]]


@pytest.mark.parametrize("pages,scans,segments,hold", [
    ([_page(), ""], [False, True], [[1], [2]], []),  # native closed with a total, then a scan
    (["", _page()], [True, False], [[1], [2]], []),  # a scan, then a native page that opens an invoice
    ([_page(), "", ""], [False, True, True], [[1], [2, 3]], []),  # a run of scans stays one part; the read looks inside
    ([_page(foot=()), ""], [False, True], [[1, 2]], [2]),  # native did not close: maybe a scanned last page
    (["", "\n".join(_rows(3))], [True, False], [[1, 2]], [2]),  # rows after a scan, no header: unsure
    (["", "Terms and conditions apply."], [True, False], [[1, 2]], []),  # no amounts: nothing to lose
    (["", ""], [True, True], [[1, 2]], []),  # all scans: nothing to read at upload
])
def test_native_and_scan_pages(pages, scans, segments, hold):
    outcome = analyse_pages(pages, scans=scans)
    assert [s.pages for s in outcome.segments] == segments and outcome.hold_pages == hold


def _scan_page(canvas_page, width=595, height=842):
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    canvas_page.drawImage(ImageReader(Image.new("RGB", (120, 170), "white")), 0, 0, width, height)
    canvas_page.showPage()


def _text_page(canvas_page, text):
    for n, line in enumerate(text.splitlines()):
        canvas_page.drawString(40, 790 - 14 * n, line)
    canvas_page.showPage()


def _pdf(*pages):
    """Each page is its text, or None for a scanned picture with no text."""
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    target = canvas.Canvas(buffer, pagesize=(595, 842))
    for page in pages:
        _scan_page(target) if page is None else _text_page(target, page)
    target.save()
    return buffer.getvalue()


def test_pdf_pages_tells_a_scan_from_a_blank_page_and_a_logo():
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    target = canvas.Canvas(buffer, pagesize=(595, 842))
    _text_page(target, _page())
    _scan_page(target)
    target.showPage()  # blank
    _scan_page(target, 60, 60)  # a small picture, not a scan
    target.save()
    assert pdf_pages(buffer.getvalue())[2] == [False, True, False, False]


def test_child_answer_carries_hold_pages(tmp_path):
    content = _pdf(_page(foot=()), None)
    outcome = analyse_pdf(content)
    assert outcome.hold_pages == [2] and _in_reader_child(content, tmp_path) == outcome


def _read_fine(*args, **kwargs):
    """The reader's part is not under test here, and the real one is slow on a page with no labels it knows."""
    return {"invoice": {"number": "X-1001", "lines": []}, "text": "made up", "boxes": [],
            "trace": [{"engine": "fake", "status": "extracted"}], "selected_engine": "fake", "completeness": 80, "document_type_hint": None,
            "extraction_note": None, "evidence": {}, "readers": {}, "annotated_evidence": {}}


def test_a_held_file_blocks_export_until_a_person_releases_it(tmp_path, monkeypatch):
    content = _pdf(_page(foot=()), _page(date="09/09/2026", foot=()))
    app, client = _studio(tmp_path, monkeypatch, _read_fine)
    with client:
        job = _upload(client, "held.pdf", content)
        assert job["several_upload"] == [2] and "may hold several invoices" in job["extraction_note"]
        assert len(client.get("/api/state").json()["jobs"]) == 1  # kept whole
        job = _settled(client, job["id"])
        jid = job["id"]
        assert job["status"] == "review", job["error"]
        assert "may hold several invoices" in job["extraction_note"]
        issue = next(i for i in job["validation"]["issues"] if i["code"] == multi_invoice.SEVERAL_CODE)
        assert issue["blocking"] and issue["level"] == "hard"
        refused = client.post(f"/api/jobs/{jid}/export", json={"revision": job["revision"]})
        assert refused.status_code == 409 and "may hold several invoices" in refused.json()["detail"]
        # Confirming review is not the release: the reviewer has to answer this one question.
        saved = client.post(f"/api/jobs/{jid}/review", json={"invoice": job["invoice"], "revision": job["revision"]})
        assert saved.status_code == 200, saved.text
        revision = saved.json()["revision"]
        selection = {"jobs": [{"id": jid, "revision": revision}]}
        assert client.post(f"/api/jobs/{jid}/export", json={"revision": revision}).status_code == 409
        batch = client.post("/api/exports/batch", json=selection)
        assert batch.status_code == 409 and "may hold several invoices" in batch.text
        draft = client.post("/api/exports/target-draft", json=selection)
        assert draft.status_code == 409 and "may hold several invoices" in draft.text
        released = client.post(f"/api/jobs/{jid}/one-invoice")
        assert released.status_code == 200, released.text
        body = released.json()
        assert not any(i["code"] == multi_invoice.SEVERAL_CODE for i in body["validation"]["issues"])
        assert "may hold several" not in (body.get("extraction_note") or "")
        assert client.post(f"/api/jobs/{jid}/one-invoice").status_code == 409
        selection = {"jobs": [{"id": jid, "revision": body["revision"]}]}
        assert client.post("/api/exports/batch", json=selection).status_code == 200
    rows = [json.loads(payload) for event, payload in audit_rows(app) if event == "several_invoices_released"]
    assert rows == [{"job_id": jid, "pages": [2], "actor": "owner-uid"}]


def test_upload_splits_invoices_with_no_readable_number_into_their_own_records(tmp_path, monkeypatch):
    content = _pdf(_page(), _page(supplier="Alcora Packaging FZE", tax="900800700600500", ref="Voucher 77"),
                   _page(ref="Doc Ref X-1002", date="02/03/2026"))
    _, client = _studio(tmp_path, monkeypatch, _read_fine)
    with client:
        _upload(client, "three.pdf", content)
        jobs = client.get("/api/state").json()["jobs"]
        assert sorted(j["split"]["part"] for j in jobs) == [1, 2, 3] and all(j["split"]["of"] == 3 for j in jobs)
        for job in jobs:
            settled = _settled(client, job["id"])
            assert settled["filename"] == f"three [{settled['split']['part']} of 3].pdf"
            assert "bound 3 invoices" in settled["extraction_note"]
            assert not any(i["code"] == multi_invoice.SEVERAL_CODE for i in settled["validation"]["issues"])


def test_read_of_a_scan_that_shows_another_invoice_holds_the_file(tmp_path, monkeypatch):
    two = [_page(), _page(supplier="Alcora Packaging FZE", tax="900800700600500", ref="Voucher 77")]

    def ocr(*args, **kwargs):
        return {"text": "\n".join(two), "boxes": [], "tables": [], "evidence": {}, "invoice": {"lines": []}, "trace": [], "provenance": [],
                "selected_engine": "paddleocr", "completeness": 0, "annotated_evidence": {}, "several_read": [2]}
    _, client = _studio(tmp_path, monkeypatch, ocr)
    with client:
        job = _settled(client, _upload(client, "scans.pdf", _pdf(None, None))["id"])
        assert job["several_read"] == [2] and "may hold several invoices" in job["extraction_note"]
        assert any(i["code"] == multi_invoice.SEVERAL_CODE for i in job["validation"]["issues"])


def test_scanned_pages_are_checked_from_what_ocr_read(tmp_path):
    from app.job_worker import several_in_read
    from PIL import Image
    two = [_page(), _page(supplier="Alcora Packaging FZE", tax="900800700600500", ref="Voucher 77")]
    boxes = [{"text": line, "page": page, "box": [10, 10 + 20 * n, 200, 25 + 20 * n], "size": [600, 800]}
             for page, text in enumerate(two, 1) for n, line in enumerate(text.splitlines())]
    file = tmp_path / "scan.pdf"
    file.write_bytes(_pdf(None, None))
    assert several_in_read(file, {"selected_engine": "paddleocr", "boxes": boxes}) == [2]
    one = [b for b in boxes if b["page"] == 1] + [{**b, "page": 2} for b in boxes if b["page"] == 1 and b["box"][1] > 150]
    assert several_in_read(file, {"selected_engine": "paddleocr", "boxes": one}) == []
    # No OCR text to look at: a multi-page scan the AI read alone cannot be checked.
    assert several_in_read(file, {"selected_engine": "openai / m", "boxes": [], "trace": [{"engine": "openai", "method": "vision_ai"}]}) == [2]
    # A multi-frame TIFF with no text.
    tiff = tmp_path / "scan.tiff"
    frames = [Image.new("RGB", (40, 40), "white") for _ in range(3)]
    frames[0].save(tiff, save_all=True, append_images=frames[1:])
    assert several_in_read(tiff, {"selected_engine": "openai / m", "boxes": [], "trace": [{"engine": "openai", "method": "vision_ai"}]}) == [2, 3]
    # A native read: the upload check covered it.
    assert several_in_read(file, {"selected_engine": "native PDF text", "boxes": boxes}) == []


def test_child_answers_with_bad_hold_pages_are_refused():
    good = {"numbers": [], "segments": [{"number": None, "pages": [1, 2]}], "refusal": None, "hold_pages": [2]}
    assert outcome_from(good).hold_pages == [2]
    for bad in ([0], [21], ["2"], "2", [True]):
        with pytest.raises(ValueError):
            outcome_from({**good, "hold_pages": bad})
