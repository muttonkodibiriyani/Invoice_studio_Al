"""The export key of an invoice with gaps (D11: such an invoice is meant to export).

A duplicate is the same number from the same supplier site. Without either one there is nothing to call a duplicate, so
the key is unique to the job: a blank number cannot collide on exports.invoice_key, and a number with no site cannot
refuse another supplier's invoice. Every test runs on SQLite. Set INV_STUDIO_TEST_DATABASE_URL to an admin connection of
a throwaway PostgreSQL to run them there too; CI has no PostgreSQL, so they skip there.
"""
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet

from app.main import create_app
from app.matching import rules_key
from app.store import Store
from tests.conftest import bearer
from tests.test_bulk_export import check, export, job, selection
from tests.test_cloud_storage_backends import FakeBlobServiceClient

ADMIN_URL = os.getenv("INV_STUDIO_TEST_DATABASE_URL")
DUPLICATE = "Invoice already exported for this supplier site"
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
        return create_app(tmp_path / "data", identity=identity, store_type=Store)
    request.getfixturevalue("postgres_url")
    from app.cloud_store import PostgresStore
    return create_app(tmp_path / "data", identity=identity,
                      store_type=lambda root: PostgresStore(root, _storage_client=FakeBlobServiceClient()))


@pytest.fixture
def invoice(client, owner, app):
    """A reviewed invoice whose rules read this site and number; ``capacity`` adds receipt evidence to allocate from."""
    def make(jid, site, number, capacity=None):
        uploads = app.state.store.root / "uploads"
        job(client, owner, app, uploads, jid, number)
        j = app.state.store.job(jid)
        fields = j["rules"]["fields"]
        fields["site"] = {**fields["site"], "value": site}
        fields["number"] = {**fields["number"], "value": number}
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


def test_keys_stored_before_this_change_block_nothing_and_still_merge_on_delete(client, owner, app, invoice):
    """Production holds rows under the old shared keys. They need no migration: new jobs never produce those keys, and
    deleting one still folds it into the tombstone of the same old key, so its allocations stay counted."""
    invoice("old-1", None, None), invoice("old-2", None, None)
    assert export_one(client, owner, app, "old-1").status_code == 200
    with app.state.store.connection(True) as c:
        c.execute("UPDATE exports SET invoice_key=? WHERE job_id=?", ("rules||", "old-1"))
    invoice("new", None, None)
    assert export_one(client, owner, app, "new").status_code == 200

    delete(client, owner, app, "old-1")
    assert export_one(client, owner, app, "old-2").status_code == 200
    with app.state.store.connection(True) as c:
        c.execute("UPDATE exports SET invoice_key=? WHERE job_id=?", ("rules||", "old-2"))
    delete(client, owner, app, "old-2")
    kept = tombstones(app)
    assert kept["old-1"][0] == "rules||"
    assert kept["old-2"][0] is None and kept["old-2"][1]["invoice_key_kept_by"] == "old-1"


def test_a_real_duplicate_exported_at_the_same_moment_is_refused_not_a_crash(client, owner, app, invoice):
    invoice("first", "S1", "INV-1"), invoice("again", "S1", "INV-1")
    with ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(lambda jid: export_one(client, owner, app, jid).status_code, ("first", "again")))
    assert codes == [200, 409]
