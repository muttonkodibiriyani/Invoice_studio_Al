"""The export key of an invoice with gaps (D11: such an invoice is meant to export).

A duplicate is the same number from the same supplier site. Without either one there is nothing to call a duplicate, so
the key is unique to the job: a blank number cannot collide on exports.invoice_key, and a number with no site cannot
refuse another supplier's invoice. Such an invoice can still be a copy, so the same file or number gives it a Possible
Duplicate hold: it exports (D11) and the receipt says so. Every test runs on SQLite. Set INV_STUDIO_TEST_DATABASE_URL to an admin connection of
a throwaway PostgreSQL to run them there too; CI has no PostgreSQL, so they skip there.
"""
import io
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import openpyxl
import pytest
from cryptography.fernet import Fernet

import app.main as main_module
from app.main import create_app
from app.models import has_unwritable_text
from app.matching import rules_key
from tests.conftest import bearer
from tests.test_batch import OPTIONS, BlobStore
from tests.test_bulk_export import check, export, job, selection
from tests.test_cloud_storage_backends import FakeBlobServiceClient

ADMIN_URL = os.getenv("INV_STUDIO_TEST_DATABASE_URL")
DUPLICATE = "Invoice already exported for this supplier site"
POSSIBLE = "Possible Duplicate"
INCOMPLETE = [(None, None), ("S1", None), (None, "INV-1"), ("S1", "   "), ("  ", "INV-1")]


@pytest.fixture
def postgres_url(monkeypatch):
    """A database of its own for one test, and the settings PostgresStore reads."""
    if not ADMIN_URL:
        pytest.skip("INV_STUDIO_TEST_DATABASE_URL is not set")
    import psycopg
    name = "export_key_" + uuid.uuid4().hex
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    monkeypatch.setenv("INV_STUDIO_DATABASE_URL", ADMIN_URL.rsplit("/", 1)[0] + "/" + name)
    monkeypatch.setenv("INV_STUDIO_BUCKET", "evidence")
    monkeypatch.setenv("INV_STUDIO_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INV_STUDIO_STORAGE_BACKEND", "azure")
    yield
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE {name} WITH (FORCE)")


@pytest.fixture(params=["sqlite", "postgres"])
def app(request, tmp_path, identity):
    if request.param == "sqlite":
        return create_app(tmp_path / "data", identity=identity, store_type=BlobStore)
    request.getfixturevalue("postgres_url")
    from app.cloud_store import PostgresStore
    return create_app(tmp_path / "data", identity=identity,
                      store_type=lambda root: PostgresStore(root, _storage_client=FakeBlobServiceClient()))


@pytest.fixture
def invoice(client, owner, app):
    """A reviewed invoice whose rules read this site and number; ``capacity`` adds receipt evidence to allocate from."""
    def make(jid, site, number, capacity=None, sha256=None):
        uploads = app.state.store.root / "uploads"
        job(client, owner, app, uploads, jid, number)
        j = app.state.store.job(jid)
        fields = j["rules"]["fields"]
        fields["site"] = {**fields["site"], "value": site}
        fields["number"] = {**fields["number"], "value": number}
        if sha256:
            j["sha256"] = sha256
        if capacity:
            j["rules"]["receipt_controls"] = {"known": True, "scope": {}, "capacities": [
                {"key": "receipt-1", "item": "1001", "qty": capacity, "reference": "synthetic"}]}
        app.state.store.job(jid, j)
        return jid
    return make


def export_one(client, owner, app, jid):
    return client.post(f"/api/jobs/{jid}/export", headers=bearer(owner), json={"revision": app.state.store.job(jid)["revision"]})


def stored_keys(app):
    with app.state.store.connection() as c:
        return {r["job_id"]: r["invoice_key"] for r in c.execute("SELECT job_id,invoice_key FROM exports").fetchall()}


def tombstones(app):
    with app.state.store.connection() as c:
        return {r["job_id"]: (r["invoice_key"], json.loads(r["payload"]))
                for r in c.execute("SELECT job_id,invoice_key,payload FROM deleted_invoices").fetchall()}


def delete(client, owner, app, *ids):
    response = client.post("/api/jobs/delete", headers=bearer(owner),
                           json={**selection(app, *ids), "confirm_permanent": True})
    assert response.status_code == 200, response.text
    return response.json()


def receipt_of(app, jid):
    with app.state.store.connection() as c:
        return json.loads(c.execute("SELECT payload FROM exports WHERE job_id=?", (jid,)).fetchone()[0])


def codes(holds):
    return [h["code"] for h in holds]


def attention(client, owner, app, jid):
    return codes(check(client, owner, app, jid)["ready"][0]["attention"])


def field(value):
    return {"value": value}


@pytest.mark.parametrize("app", ["sqlite"], indirect=True)
def test_only_a_site_and_a_number_make_a_key_two_jobs_can_share(app):
    def key(site, number, jid="job-1"):
        return rules_key({"fields": {"site": field(site), "number": field(number)}}, jid)
    assert key("S1", " inv-1 ") == key("S1", "INV-1", "job-2") == "rules|S1|INV-1"
    for site, number in INCOMPLETE:
        assert key(site, number) == "rules-job|job-1"
        assert key(site, number, "job-2") == "rules-job|job-2"


@pytest.mark.parametrize("site,number", INCOMPLETE)
def test_two_different_invoices_with_gaps_both_export_one_at_a_time(client, owner, app, invoice, site, number):
    first, second = invoice("first", site, number), invoice("second", site, number)
    for jid in (first, second):
        response = export_one(client, owner, app, jid)
        assert response.status_code == 200, response.text
        assert client.get(response.json()["url"], headers=bearer(owner)).status_code == 200
    assert stored_keys(app) == {"first": "rules-job|first", "second": "rules-job|second"}


@pytest.mark.parametrize("site,number", INCOMPLETE)
def test_two_different_invoices_with_gaps_both_export_in_one_selection(client, owner, app, invoice, site, number):
    first, second = invoice("first", site, number), invoice("second", site, number)
    result = check(client, owner, app, first, second)
    assert [r["id"] for r in result["ready"]] == [first, second] and result["blocked"] == []
    response = export(client, owner, app, first, second)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 2
    assert client.get(response.json()["url"], headers=bearer(owner)).status_code == 200
    assert stored_keys(app) == {"first": "rules-job|first", "second": "rules-job|second"}


def test_a_number_read_without_a_site_does_not_refuse_the_supplier_that_has_one(client, owner, app, invoice):
    for jid, site in (("no-site", None), ("site-1", "S1"), ("site-2", "S2")):
        invoice(jid, site, "INV-1")
        assert export_one(client, owner, app, jid).status_code == 200
    assert stored_keys(app) == {"no-site": "rules-job|no-site", "site-1": "rules|S1|INV-1", "site-2": "rules|S2|INV-1"}


def test_a_real_duplicate_is_refused_with_its_message_on_the_single_export(client, owner, app, invoice):
    invoice("first", "S1", "INV-1"), invoice("again", "S1", " inv-1 ")
    assert export_one(client, owner, app, "first").status_code == 200
    response = export_one(client, owner, app, "again")
    assert (response.status_code, response.json()["detail"]) == (409, DUPLICATE)
    assert not app.state.store.job("again").get("export_id")
    assert set(stored_keys(app)) == {"first"}


def test_a_real_duplicate_is_refused_in_one_selection_and_across_selections(client, owner, app, invoice):
    invoice("first", "S1", "INV-1"), invoice("again", "S1", "INV-1"), invoice("blank", "S1", None)
    result = check(client, owner, app, "first", "again", "blank")
    assert [r["id"] for r in result["ready"]] == ["first", "blank"]
    assert [(b["id"], b["reasons"][0]["message"]) for b in result["blocked"]] == [("again", DUPLICATE)]

    refused = export(client, owner, app, "first", "again")
    assert refused.status_code == 409 and DUPLICATE in refused.json()["detail"]
    assert stored_keys(app) == {}  # all or nothing

    assert export(client, owner, app, "first").status_code == 200
    refused = export(client, owner, app, "again")
    assert refused.status_code == 409 and DUPLICATE in refused.json()["detail"]
    assert export(client, owner, app, "blank").status_code == 200


def test_exporting_the_same_job_again_keeps_its_one_export(client, owner, app, invoice):
    invoice("blank", None, None)
    first = export_one(client, owner, app, "blank")
    assert export_one(client, owner, app, "blank").json()["id"] == first.json()["id"]
    assert stored_keys(app) == {"blank": "rules-job|blank"}
    refused = export(client, owner, app, "blank")
    assert refused.status_code == 409 and "already exported" in refused.json()["detail"]


def test_deleted_invoices_with_gaps_keep_their_own_tombstones_and_receipts(client, owner, app, invoice):
    invoice("first", None, None, capacity="5000"), invoice("second", None, None, capacity="5000")
    assert export_one(client, owner, app, "first").status_code == 200
    assert export_one(client, owner, app, "second").status_code == 200
    assert delete(client, owner, app, "first", "second")["ledger_tombstones"] == 2

    kept = tombstones(app)
    assert {jid: key for jid, (key, _) in kept.items()} == {"first": "rules-job|first", "second": "rules-job|second"}
    assert all(payload["allocations"] == [{"key": "receipt-1", "qty": "2002"}] for _, payload in kept.values())
    with app.state.store.connection() as c:
        assert len(app.state.store.ledger(c)) == 2  # the receipt control still counts both

    invoice("third", None, None)
    assert export_one(client, owner, app, "third").status_code == 200


def test_a_deleted_batch_of_invoices_with_gaps_keeps_a_tombstone_each(client, owner, app, invoice):
    invoice("first", "S1", None), invoice("second", None, "INV-1")
    assert export(client, owner, app, "first", "second").status_code == 200
    assert delete(client, owner, app, "first", "second")["deleted_batches"] == 1
    assert {jid: key for jid, (key, _) in tombstones(app).items()} == {"first": "rules-job|first", "second": "rules-job|second"}


def test_a_deleted_real_invoice_still_refuses_its_duplicate(client, owner, app, invoice):
    invoice("first", "S1", "INV-1")
    assert export_one(client, owner, app, "first").status_code == 200
    delete(client, owner, app, "first")
    invoice("again", "S1", "INV-1")
    response = export_one(client, owner, app, "again")
    assert (response.status_code, response.json()["detail"]) == (409, DUPLICATE)


def make_old(app, jid, key):
    """Rewrite an export as main wrote it: the old key in the column and the receipt, no copy signal in the receipt."""
    with app.state.store.connection(True) as c:
        receipt = json.loads(c.execute("SELECT payload FROM exports WHERE job_id=?", (jid,)).fetchone()[0])
        receipt["invoice_key"] = key
        del receipt["sha256"], receipt["document"]
        c.execute("UPDATE exports SET invoice_key=?,payload=? WHERE job_id=?", (key, json.dumps(receipt), jid))


def test_keys_stored_before_this_change_block_nothing_and_still_merge_on_delete(client, owner, app, invoice):
    """Production holds rows under the old shared keys. They need no migration: new jobs never produce those keys, and
    deleting one still folds it into the tombstone of the same old key, so its allocations stay counted."""
    invoice("old-1", None, None), invoice("old-2", None, None)
    assert export_one(client, owner, app, "old-1").status_code == 200
    make_old(app, "old-1", "rules||")
    invoice("new", None, None)
    assert export_one(client, owner, app, "new").status_code == 200

    delete(client, owner, app, "old-1")
    assert export_one(client, owner, app, "old-2").status_code == 200
    make_old(app, "old-2", "rules||")
    delete(client, owner, app, "old-2")
    kept = tombstones(app)
    assert kept["old-1"][0] == "rules||"
    assert kept["old-2"][0] is None and kept["old-2"][1]["invoice_key_kept_by"] == "old-1"


def test_receipts_read_from_the_ledger_under_old_keys(client, owner, app, invoice):
    """The ledger reads the receipt, not the column. An old complete key still refuses. An old key without a site or a
    number no longer protects its number: it stored no copy signal, so that is neither refused nor held."""
    invoice("old-full", "S1", "INV-2"), invoice("old-site", None, "INV-1")
    assert export_one(client, owner, app, "old-full").status_code == 200
    assert export_one(client, owner, app, "old-site").status_code == 200
    make_old(app, "old-full", "rules|S1|INV-2"), make_old(app, "old-site", "rules||INV-1")

    invoice("real-copy", "S1", "INV-2"), invoice("no-signal", None, "INV-1")
    response = export_one(client, owner, app, "real-copy")
    assert (response.status_code, response.json()["detail"]) == (409, DUPLICATE)
    assert export_one(client, owner, app, "no-signal").status_code == 200
    assert codes(receipt_of(app, "no-signal")["open_holds"]) == []


def test_a_real_duplicate_exported_at_the_same_moment_is_refused_not_a_crash(client, owner, app, invoice):
    invoice("first", "S1", "INV-1"), invoice("again", "S1", "INV-1")
    with ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(lambda jid: export_one(client, owner, app, jid).status_code, ("first", "again")))
    assert codes == [200, 409]


# Review finding 2, routes a to d: each second copy exports (D11) with the hold on its check and its receipt.
def test_route_a_blank_first_then_the_same_file_typed_in_full(client, owner, app, invoice):
    invoice("blank", None, None, sha256="same-file"), invoice("typed", "S1", "INV-5", sha256="same-file")
    assert export_one(client, owner, app, "blank").status_code == 200
    assert POSSIBLE in attention(client, owner, app, "typed")
    assert export_one(client, owner, app, "typed").status_code == 200
    assert POSSIBLE in codes(receipt_of(app, "typed")["open_holds"])
    assert stored_keys(app)["typed"] == "rules|S1|INV-5"


def test_route_a_a_number_without_a_site_first_then_the_full_key(client, owner, app, invoice):
    invoice("no-site", None, "INV-5"), invoice("typed", "S1", "inv-5")
    assert export_one(client, owner, app, "no-site").status_code == 200
    assert export_one(client, owner, app, "typed").status_code == 200
    assert POSSIBLE in codes(receipt_of(app, "typed")["open_holds"])


def test_route_b_full_first_then_a_blank_copy_of_the_same_file(client, owner, app, invoice):
    invoice("full", "S1", "INV-5", sha256="same-file"), invoice("blank", None, None, sha256="same-file")
    assert export_one(client, owner, app, "full").status_code == 200
    assert export_one(client, owner, app, "blank").status_code == 200
    assert POSSIBLE in codes(receipt_of(app, "blank")["open_holds"])


def test_route_c_clearing_the_number_of_a_refused_copy_keeps_a_hold(client, owner, app, invoice):
    invoice("first", "S1", "INV-5", sha256="same-file"), invoice("copy", "S1", "INV-5", sha256="same-file")
    assert export_one(client, owner, app, "first").status_code == 200
    assert export_one(client, owner, app, "copy").status_code == 409
    j = app.state.store.job("copy")
    j["rules"]["fields"]["number"]["value"] = None
    app.state.store.job("copy", j)
    assert export_one(client, owner, app, "copy").status_code == 200
    assert POSSIBLE in codes(receipt_of(app, "copy")["open_holds"])


def test_route_d_the_same_number_without_a_site_holds_and_never_refuses(client, owner, app, invoice):
    invoice("one", None, "INV-5"), invoice("two", None, " inv-5 "), invoice("sited", "S1", "INV-5")
    for jid in ("one", "two", "sited"):
        assert export_one(client, owner, app, jid).status_code == 200
    assert codes(receipt_of(app, "one")["open_holds"]) == []
    assert POSSIBLE in codes(receipt_of(app, "two")["open_holds"])
    assert POSSIBLE in codes(receipt_of(app, "sited")["open_holds"])


def test_the_same_number_under_two_known_sites_is_two_suppliers_not_a_hold(client, owner, app, invoice):
    invoice("one", "S1", "INV-5"), invoice("two", "S2", "INV-5")
    assert export_one(client, owner, app, "one").status_code == 200
    assert export_one(client, owner, app, "two").status_code == 200
    assert codes(receipt_of(app, "two")["open_holds"]) == []


def test_what_the_hold_cannot_catch_a_re_scan_with_no_number(client, owner, app, invoice):
    invoice("first", "S1", "INV-5", sha256="scan-1"), invoice("re-scan", "S1", None, sha256="scan-2")
    assert export_one(client, owner, app, "first").status_code == 200
    assert export_one(client, owner, app, "re-scan").status_code == 200
    assert codes(receipt_of(app, "re-scan")["open_holds"]) == []


def test_a_review_confirm_does_not_clear_the_hold_or_mark_the_copy_ready(client, owner, app, invoice):
    invoice("first", None, "INV-5"), invoice("copy", None, "INV-5")
    assert export_one(client, owner, app, "first").status_code == 200
    shown = client.get("/api/jobs/copy", headers=bearer(owner)).json()
    assert shown["reviewed"] is True and shown["status"] == "review" and shown["validation"]["ready"] is False
    assert POSSIBLE in codes(shown["validation"]["issues"])
    assert client.get("/api/jobs/first", headers=bearer(owner)).json()["validation"]["issues"] == []  # exported: nothing to warn


def test_a_copy_in_the_same_selection_exports_with_the_hold(client, owner, app, invoice):
    invoice("one", None, "INV-5"), invoice("two", None, "INV-5")
    result = check(client, owner, app, "one", "two")
    assert [codes(r["attention"]) for r in result["ready"]] == [[], [POSSIBLE]]
    assert export(client, owner, app, "one", "two").status_code == 200
    assert POSSIBLE in codes(receipt_of(app, "two")["open_holds"])


def test_a_deleted_invoice_keeps_the_signal_on_its_tombstone(client, owner, app, invoice):
    invoice("blank", None, None, sha256="same-file"), invoice("no-site", None, "INV-5")
    assert export_one(client, owner, app, "blank").status_code == 200
    assert export_one(client, owner, app, "no-site").status_code == 200
    delete(client, owner, app, "blank", "no-site")
    kept = tombstones(app)
    assert kept["blank"][1]["sha256"] == "same-file" and kept["no-site"][1]["document"] == "INV-5"
    invoice("again", None, None, sha256="same-file"), invoice("number", None, "INV-5")
    assert POSSIBLE in attention(client, owner, app, "again")
    assert POSSIBLE in attention(client, owner, app, "number")


def status_of(client, owner, jid):
    shown = client.get(f"/api/jobs/{jid}", headers=bearer(owner)).json()
    return shown["status"], shown["validation"]["ready"]  # both must hold for Select approved (isBatchReady in app.js)


def test_select_approved_takes_both_copies_before_the_original_exports(client, owner, app, invoice):
    invoice("one", None, "INV-5"), invoice("two", None, "INV-5")
    assert [status_of(client, owner, jid) for jid in ("one", "two")] == [("ready", True), ("ready", True)]
    assert [codes(r["attention"]) for r in check(client, owner, app, "one", "two")["ready"]] == [[], [POSSIBLE]]
    assert export(client, owner, app, "one", "two").status_code == 200
    assert codes(receipt_of(app, "one")["open_holds"]) == [] and POSSIBLE in codes(receipt_of(app, "two")["open_holds"])


def test_select_approved_skips_the_copy_after_the_original_exports_but_manual_selection_takes_it(client, owner, app, invoice):
    invoice("one", None, "INV-5"), invoice("two", None, "INV-5")
    assert status_of(client, owner, "two") == ("ready", True)
    assert export_one(client, owner, app, "one").status_code == 200
    assert status_of(client, owner, "two") == ("review", False)  # nobody acted on it: the original's export flips it
    assert POSSIBLE in codes(check(client, owner, app, "two")["ready"][0]["attention"])
    assert export(client, owner, app, "two").status_code == 200
    assert POSSIBLE in codes(receipt_of(app, "two")["open_holds"])


def test_a_batch_receipt_keeps_the_signal_so_a_later_copy_is_held(client, owner, app, invoice):
    invoice("bulk", None, "INV-5", sha256="same-file")
    assert export(client, owner, app, "bulk").status_code == 200
    kept = receipt_of(app, "bulk")
    assert (kept["sha256"], kept["document"], kept["filename"]) == ("same-file", "INV-5", "bulk.pdf")
    invoice("file", None, None, sha256="same-file"), invoice("number", None, "INV-5")
    assert POSSIBLE in attention(client, owner, app, "file")
    assert POSSIBLE in attention(client, owner, app, "number")


def test_two_exported_copies_have_nothing_left_to_warn(client, owner, app, invoice):
    invoice("one", None, "INV-5"), invoice("two", None, "INV-5")
    assert export_one(client, owner, app, "one").status_code == 200
    assert export_one(client, owner, app, "two").status_code == 200
    for jid in ("one", "two"):
        shown = client.get(f"/api/jobs/{jid}", headers=bearer(owner)).json()
        assert shown["status"] == "exported" and POSSIBLE not in codes(shown["validation"]["issues"])


def message_of(app, jid):
    return next(h["message"] for h in receipt_of(app, jid)["open_holds"] if h["code"] == POSSIBLE)


def test_the_hold_names_the_match_and_never_asks_for_a_confirm(client, owner, app, invoice):
    invoice("first", None, "INV-5", sha256="same-file"), invoice("copy", None, None, sha256="same-file"), invoice("number", None, "inv-5")
    assert export_one(client, owner, app, "first").status_code == 200
    assert export_one(client, owner, app, "copy").status_code == 200
    assert export_one(client, owner, app, "number").status_code == 200
    day = receipt_of(app, "first")["created_at"][:10]
    assert message_of(app, "copy") == f"Possible copy of first.pdf, exported {day}: the same file. Export anyway or delete this one."
    assert "the same invoice number, INV-5" in message_of(app, "number")


def test_a_deleted_match_is_named_as_that_because_a_tombstone_keeps_no_filename(client, owner, app, invoice):
    invoice("first", None, "INV-5")
    assert export_one(client, owner, app, "first").status_code == 200
    delete(client, owner, app, "first")
    assert "filename" not in tombstones(app)["first"][1]
    invoice("again", None, "INV-5")
    assert export_one(client, owner, app, "again").status_code == 200
    assert message_of(app, "again").startswith("Possible copy of an export since deleted: the same invoice number, INV-5.")


def test_the_hold_is_listed_first(client, owner, app, invoice):
    invoice("first", None, "INV-5"), invoice("copy", None, "INV-5")
    assert export_one(client, owner, app, "first").status_code == 200
    j = app.state.store.job("copy")
    j["rules"]["issues"] = [{"code": "Blank Cell", "message": "A cell is blank", "owner": "", "line": 1, "blocking": True, "level": "hard"}]
    app.state.store.job("copy", j)
    assert attention(client, owner, app, "copy") == [POSSIBLE, "Blank Cell"]
    assert export_one(client, owner, app, "copy").status_code == 200
    assert codes(receipt_of(app, "copy")["open_holds"]) == [POSSIBLE, "Blank Cell"]


# Review finding 5: through the real rules path, not a hand-set view.
def real_job(client, owner, app, jid, number=None):
    source = app.state.store.root / "uploads" / f"{jid}.pdf"
    source.write_bytes(b"%PDF-1.4")
    app.state.store.job(jid, {"id": jid, "filename": f"{jid}.pdf", "path": str(source), "status": "review", "revision": 1,
                              "reviewed": True, "provenance": [], "invoice": {"number": number, "lines": [{}, {}]}})
    assert client.get(f"/api/jobs/{jid}", headers=bearer(owner)).status_code == 200


def save(client, owner, app, jid, number=None, entries=None):
    j = app.state.store.job(jid)
    body = {"invoice": {**j["invoice"], **({"number": number} if number is not None else {})},
            "revision": j["revision"], "confirm": True, **({"entries": entries} if entries is not None else {})}
    # json.dumps escapes a lone surrogate, which the test client's own encoder cannot send.
    return client.post(f"/api/jobs/{jid}/review", headers={**bearer(owner), "Content-Type": "application/json"},
                       content=json.dumps(body))


def test_a_site_and_number_typed_at_review_make_the_real_key(client, owner, app):
    for jid, number, site in (("first", " inv-7 ", " 101 "), ("again", "INV-7", "101")):
        real_job(client, owner, app, jid)
        assert save(client, owner, app, jid, entries={"header": {"number": number, "site": site}}).status_code == 200
    assert export_one(client, owner, app, "first").status_code == 200
    assert stored_keys(app)["first"] == "rules|101|INV-7"
    response = export_one(client, owner, app, "again")
    assert (response.status_code, response.json()["detail"]) == (409, DUPLICATE)
    real_job(client, owner, app, "no-site")
    assert save(client, owner, app, "no-site", entries={"header": {"number": "inv-7"}}).status_code == 200
    assert export_one(client, owner, app, "no-site").status_code == 200
    assert POSSIBLE in codes(receipt_of(app, "no-site")["open_holds"])


# Review finding 3: a value the workbook or PostgreSQL cannot hold.
BAD_TEXT = {"nul": "A\x00B", "soh": "A\x01B", "vertical-tab": "A\x0bB", "escape": "A\x1bB", "non-character": "A\ufffeB",
            "surrogate": "A\ud800B"}


@pytest.mark.parametrize("name", list(BAD_TEXT))
def test_a_typed_value_excel_cannot_hold_is_refused_at_the_save(client, owner, app, name):
    real_job(client, owner, app, "a")
    before = app.state.store.job("a")["revision"]
    for attempt in ({"number": BAD_TEXT[name]}, {"entries": {"header": {"number": BAD_TEXT[name]}}}):
        response = save(client, owner, app, "a", **attempt)
        if name == "surrogate" and "number" in attempt:  # the request model refuses this one first, as clean
            assert response.status_code == 422, response.text
        else:
            assert response.status_code == 400 and "cannot be saved" in response.json()["detail"], response.text
    assert app.state.store.job("a")["revision"] == before


@pytest.mark.parametrize("name", list(BAD_TEXT))
def test_a_value_the_reader_sends_is_cleaned_and_the_invoice_exports(client, owner, app, monkeypatch, name):
    def reader(path, options, store, ai_reader, progress, **context):
        return {"invoice": {"number": BAD_TEXT[name], "lines": []}, "text": "INVOICE\x0cInvoice No: A" + BAD_TEXT[name] + "B\nSupplier: X" + BAD_TEXT[name] + "Y\n", "boxes": [],
                "trace": [{"engine": "fake", "status": "extracted"}], "selected_engine": "fake", "completeness": 40,
                "document_type_hint": None, "extraction_note": None, "evidence": {}, "readers": {}, "annotated_evidence": {}}
    monkeypatch.setattr(main_module, "process_job", reader)
    data = b"synthetic text invoice"
    plan = client.post("/api/preflight", headers=bearer(owner),
                       json={"options": OPTIONS, "files": [{"name": "a.txt", "size": len(data)}]}).json()["token"]
    assert client.post("/api/preflight/confirm", json={"token": plan}, headers=bearer(owner)).status_code == 200
    jid = client.post("/api/invoices", files={"file": ("a.txt", data, "text/plain")}, headers=bearer(owner),
                      data={"options": json.dumps(OPTIONS), "preflight_token": plan}).json()["id"]
    for _ in range(100):
        if app.state.store.job(jid)["status"] not in ("queued", "processing"):
            break
        time.sleep(.1)
    shown = client.get(f"/api/jobs/{jid}", headers=bearer(owner)).json()
    assert shown["status"] != "error" and shown["invoice"]["number"] == "AB", shown.get("error")
    assert shown["text"].startswith("INVOICE\x0c")  # the page mark stays: it only cannot go in a cell
    assert not has_unwritable_text([shown["invoice"], shown["rules"]["fields"]])  # so a save sends back nothing it refuses
    assert save(client, owner, app, jid, number="AB").status_code == 200
    draft = client.post(f"/api/jobs/{jid}/extraction-draft", headers=bearer(owner), json={"revision": app.state.store.job(jid)["revision"], "acknowledge_unvalidated": True})
    assert draft.status_code == 200, draft.text
    assert export_one(client, owner, app, jid).status_code == 200


@pytest.mark.parametrize("name", ["soh", "vertical-tab", "escape", "non-character"])  # NUL and surrogates: PostgreSQL never stored them
def test_an_invoice_read_before_this_check_is_served_clean_and_exports(client, owner, app, name):
    real_job(client, owner, app, "a", BAD_TEXT[name])
    assert client.get("/api/jobs/a", headers=bearer(owner)).json()["invoice"]["number"] == "AB"
    assert export_one(client, owner, app, "a").status_code == 200
    real_job(client, owner, app, "b", BAD_TEXT[name])
    assert export(client, owner, app, "b").status_code == 200


def test_a_view_stored_before_the_check_still_exports_cleaned(client, owner, app, invoice):
    invoice("stored", "S1", "A\x01B\ufffe")
    response = export_one(client, owner, app, "stored")
    assert response.status_code == 200, response.text
    sheet = openpyxl.load_workbook(io.BytesIO(client.get(response.json()["url"], headers=bearer(owner)).content))["Header"]
    assert sheet["B2"].value == "AB"
