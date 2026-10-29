"""Synthetic workbook/API tests; private cell values must never enter reports/logs."""
import io
import json
from datetime import date
from zipfile import ZipFile, ZIP_DEFLATED

import openpyxl
import pytest

from app import mapping_headers, mapping_tables
from app.fine_rules_tables import load
from app.models import Invoice
from tests.conftest import audit_rows, bearer

BASE = "/api/mapping-tables"
SECRET = "synthetic-private-cell-marker"


def workbook(edit=None, country="ARE"):
    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, rows in {
        "VAT CODES": [
            ["VAT_TYPE", "VAT_CODE", "VAT_REGION_NAME", "VAT_RATE", "ACTIVE_DATE"],
            ["C", "PV5", "ARE Vat Region", 5, date(2026, 1, 1)],
            ["X", "OTHER", SECRET, 5, date(2026, 1, 1)],
        ],
        "LOCATIONS": [["LOCATION", "WH/STORE", "COUNTRY", "ENTITY AND CURENCY"], ["8001", "W/H", country, "RA4 AED"]],
        "SUPPLIER SITES": [["SUPPLIER SITE", "STATUS DESCRIPTION", "CURRENCY", "SUPPLIER NAME"],
                           ["SITE1", "Active", "AED", SECRET]],
    }.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
    if edit:
        edit(book)
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def upload(client, owner, content=None, filename="mappings.xlsx"):
    return client.post(BASE + "/validate", headers=bearer(owner),
                       files={"file": (filename, content if content is not None else workbook(), mapping_tables.MIME)})


def activate(client, owner, upload_id):
    return client.post(BASE + f"/uploads/{upload_id}/activate", headers=bearer(owner))


def history(client, owner):
    return client.get(BASE, headers=bearer(owner)).json()


def test_valid_upload_activation_download_and_reupload(client, owner, app, caplog):
    initial = {"buyer_name": "Synthetic buyer", "qty_tolerance": "2", "version": "before-upload"}
    app.state.store.set("fine_rules_config", initial)
    content = workbook()
    response = upload(client, owner, content, "../synthetic mapping.xlsx")
    assert response.status_code == 200
    result = response.json()
    assert result["filename"] == "synthetic mapping.xlsx"
    assert result["report"]["valid"] is True
    assert result["report"]["sheets"]["VAT CODES"] == {"rows": 2, "accepted": 1, "excluded": 1, "flagged": 0}
    assert SECRET not in response.text
    assert app.state.store.get("fine_rules_config") == initial
    assert history(client, owner)["versions"] == []
    assert activate(client, owner, result["upload_id"]).status_code == 200
    config = app.state.store.get("fine_rules_config")
    assert config["buyer_name"] == initial["buyer_name"] and config["qty_tolerance"] == "2"
    assert config["location_master"]["8001"]["entity_currency"] == "RA4 AED"
    versions = history(client, owner)
    assert len(versions["versions"]) == 2
    assert versions["active"]["id"] == result["upload_id"]
    metadata = next(v for v in versions["versions"] if v["id"] == result["upload_id"])
    assert metadata["created_by"] == "owner@example.test" and metadata["created_at"]
    download = client.get(BASE + "/active/workbook", headers=bearer(owner))
    assert download.content == content
    assert "synthetic%20mapping.xlsx" in download.headers["content-disposition"]
    assert activate(client, owner, result["upload_id"]).status_code == 404
    second = upload(client, owner).json()["upload_id"]
    assert second != result["upload_id"]
    assert activate(client, owner, second).status_code == 200
    assert len(history(client, owner)["versions"]) == 3
    logs = json.dumps(audit_rows(app)) + caplog.text
    assert SECRET not in logs and "synthetic mapping.xlsx" not in logs


def test_rollback_preserves_other_settings_and_restores_initial(client, owner, app):
    store = app.state.store
    store.set("fine_rules_config", {"buyer_name": "before", "version": "baseline"})
    first = upload(client, owner).json()["upload_id"]
    activate(client, owner, first)
    second = upload(client, owner, workbook(country="SAU")).json()["upload_id"]
    activate(client, owner, second)
    updated = store.get("fine_rules_config")
    updated["buyer_name"] = "after"
    store.set("fine_rules_config", updated)
    assert client.post(BASE + f"/versions/{first}/rollback", headers=bearer(owner)).status_code == 200
    assert store.get("fine_rules_config")["location_master"]["8001"]["country"] == "ARE"
    assert store.get("fine_rules_config")["buyer_name"] == "after"
    baseline = next(v for v in history(client, owner)["versions"] if v["source"] == "snapshot")
    assert client.post(BASE + f"/versions/{baseline['id']}/rollback", headers=bearer(owner)).status_code == 200
    assert store.get("fine_rules_config") == {"buyer_name": "after", "version": "baseline"}
    assert client.get(BASE + "/active/workbook", headers=bearer(owner)).status_code == 404


def test_bad_sheets_headers_and_rows_report_all_problems(client, owner, app, caplog):
    def edit(book):
        book.remove(book["VAT CODES"])
        book["LOCATIONS"]["A1"] = SECRET
        book["SUPPLIER SITES"].append(["SITE2", "Wrong", "INVALID", SECRET])
        book["SUPPLIER SITES"].append([None, "Active", "AED"])
    result = upload(client, owner, workbook(edit)).json()
    assert result["upload_id"] is None and not result["report"]["valid"]
    flagged = result["report"]["flagged"]
    assert {(f["sheet"], f["row"]) for f in flagged} == {
        ("VAT CODES", 1), ("LOCATIONS", 1), ("SUPPLIER SITES", 3), ("SUPPLIER SITES", 4)}
    assert result["report"]["sheets"]["SUPPLIER SITES"]["rows"] == 3
    assert SECRET not in json.dumps(result) + json.dumps(audit_rows(app)) + caplog.text
    assert not app.state.store.get("fine_rules_config")
    assert activate(client, owner, "missing").status_code == 404


@pytest.mark.parametrize("change", ["empty", "duplicate_row", "duplicate_header", "formula", "date", "rate"])
def test_invalid_mapping_data_cannot_activate(change, client, owner):
    def edit(book):
        if change == "empty":
            book["LOCATIONS"].delete_rows(2)
        elif change == "duplicate_row":
            book["LOCATIONS"].append(["8001", "STORE", "ARE"])
        elif change == "duplicate_header":
            book["LOCATIONS"]["E1"] = "ENTITY AND CURRENCY"
        elif change == "formula":
            book["LOCATIONS"]["A2"] = '=HYPERLINK("' + SECRET + '")'
        elif change == "date":
            book["VAT CODES"]["E2"] = SECRET
        else:
            book["VAT CODES"]["D2"] = -1
    result = upload(client, owner, workbook(edit)).json()
    assert not result["report"]["valid"] and result["upload_id"] is None
    assert SECRET not in json.dumps(result)


def test_header_adapter_aliases_and_blank_rows(monkeypatch):
    aliases = mapping_headers.SHEETS["SUPPLIER SITES"]["headers"]
    monkeypatch.setitem(aliases, "CURRENCY", ("CURRENCY", "Site Currency"))
    def edit(book):
        book["SUPPLIER SITES"]["C1"] = " site currency "
        book["LOCATIONS"].append([None, None, None])
    tables, report = load(io.BytesIO(workbook(edit)))
    assert report["valid"] and report["sheets"]["LOCATIONS"]["rows"] == 1
    assert tables["supplier_sites"][0]["currency"] == "AED"


@pytest.mark.parametrize("filename,content,status", [
    ("mapping.xlsm", b"test", 415), ("mapping.xls", b"test", 415),
    ("mapping.xlsx", b"broken-private-cell-marker", 400),
    ("mapping.xlsx", b"x" * (mapping_tables.MAX_BYTES + 1), 413),
])
def test_file_type_and_size_limits(filename, content, status, client, owner):
    result = upload(client, owner, content, filename)
    assert result.status_code == status
    assert "private-cell-marker" not in result.text


def test_renamed_macros_and_zip_expansion_rejected(client, owner, monkeypatch):
    content = io.BytesIO()
    with ZipFile(content, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "application/vnd.ms-excel.sheet.macroEnabled.main+xml")
        archive.writestr("xl/vbaProject.bin", "private-cell-marker")
    assert upload(client, owner, content.getvalue()).status_code == 415
    monkeypatch.setattr(mapping_tables, "MAX_EXPANDED_BYTES", 1)
    assert upload(client, owner).status_code == 413


def test_expired_pending_upload_cannot_activate(client, owner, app):
    upload_id = upload(client, owner).json()["upload_id"]
    with app.state.store.connection(True) as c:
        c.execute("UPDATE mapping_table_versions SET created_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", upload_id))
    assert activate(client, owner, upload_id).status_code == 409
    assert not app.state.store.get("fine_rules_config")


def test_non_admin_is_forbidden_on_every_mapping_endpoint(client, tenant):
    token = tenant.token("mapping-member", "mapping-member@example.test")
    for method, path in [("GET", BASE), ("POST", BASE + "/validate"),
                         ("POST", BASE + "/uploads/x/activate"), ("POST", BASE + "/versions/x/rollback"),
                         ("GET", BASE + "/active/workbook")]:
        assert client.request(method, path, headers=bearer(token)).status_code == 403


def test_config_api_override_is_detected_and_snapshotted(client, owner, app):
    upload_id = upload(client, owner).json()["upload_id"]
    activate(client, owner, upload_id)
    response = client.post("/api/fine-rules/config", headers=bearer(owner), json={"version": "external", "buyer_name": "changed"})
    assert response.status_code == 200
    assert history(client, owner)["active"] is None
    assert client.get(BASE + "/active/workbook", headers=bearer(owner)).status_code == 404
    upload_id = upload(client, owner).json()["upload_id"]
    activate(client, owner, upload_id)
    assert any(v["version"] == "external" for v in history(client, owner)["versions"])


def test_invoice_results_refresh_after_activation_and_rollback(client, owner, app):
    store = app.state.store
    store.job("mapping-invoice", {"id": "mapping-invoice", "filename": "synthetic.txt", "status": "review",
                                "revision": 1, "reviewed": False, "invoice": Invoice(number="TEST").model_dump(mode="json")})
    before = client.get("/api/jobs/mapping-invoice", headers=bearer(owner))
    assert before.status_code == 200
    before_rules = before.json()["rules"]
    upload_id = upload(client, owner).json()["upload_id"]
    activate(client, owner, upload_id)
    after = client.get("/api/jobs/mapping-invoice", headers=bearer(owner)).json()["rules"]
    assert before_rules["signature"] != after["signature"]
    assert before_rules["computed_at"] != after["computed_at"]
    baseline = next(v for v in history(client, owner)["versions"] if v["source"] == "snapshot")
    client.post(BASE + f"/versions/{baseline['id']}/rollback", headers=bearer(owner))
    restored = client.get("/api/jobs/mapping-invoice", headers=bearer(owner)).json()["rules"]
    assert restored["signature"] == before_rules["signature"]
    assert restored["computed_at"] != after["computed_at"]


def test_mapping_ui_workflow():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the offline UI behavior check")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests/browser_mapping_tables.cjs"), str(root / "app/static/app.js")],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_failed_activation_transaction_keeps_previous_config(client, owner, app, monkeypatch):
    store = app.state.store
    first = upload(client, owner).json()["upload_id"]
    activate(client, owner, first)
    before = store.get("fine_rules_config")
    pending = upload(client, owner, workbook(country="SAU")).json()["upload_id"]
    original_audit = store.audit
    def fail(event, payload, c=None):
        if event == "mapping_tables_activated":
            raise RuntimeError("synthetic audit failure")
        return original_audit(event, payload, c)
    monkeypatch.setattr(store, "audit", fail)
    response = activate(client, owner, pending)
    assert response.status_code == 500 and "synthetic audit failure" not in response.text
    assert store.get("fine_rules_config") == before
    assert history(client, owner)["active"]["id"] == first
    assert len(history(client, owner)["versions"]) == 2


def test_parser_exception_never_echoes_cell_data(client, owner, caplog, monkeypatch):
    def fail(_):
        raise ValueError(SECRET)
    monkeypatch.setattr(mapping_tables, "load", fail)
    response = upload(client, owner)
    assert response.status_code == 400
    assert SECRET not in response.text + caplog.text


def test_versions_on_local_postgres(tmp_path, monkeypatch):
    """Exercise the production store/transaction adapter using an isolated Unix socket database."""
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.cloud_store import PostgresStore

    candidates = list(Path("/usr/lib/postgresql").glob("*/bin/initdb"))
    initdb = shutil.which("initdb") or (str(sorted(candidates)[-1]) if candidates else None)
    if not initdb or os.geteuid() == 0:
        pytest.skip("A local non-root PostgreSQL installation is needed")
    pg_ctl = str(Path(initdb).with_name("pg_ctl"))
    with tempfile.TemporaryDirectory(prefix="mapping-pg-") as directory:
        data = str(Path(directory) / "db")
        subprocess.run([initdb, "-D", data, "-A", "trust", "--no-locale", "--encoding=UTF8"], check=True, capture_output=True)
        subprocess.run([pg_ctl, "-D", data, "-l", str(Path(directory) / "server.log"),
                        "-o", f"-h '' -k {directory} -p 6543", "-w", "start"], check=True, capture_output=True)
        try:
            monkeypatch.setenv("INV_STUDIO_DATABASE_URL", f"host={directory} port=6543 dbname=postgres")
            monkeypatch.setenv("INV_STUDIO_BUCKET", "synthetic-unused")
            monkeypatch.setenv("INV_STUDIO_VAULT_KEY", Fernet.generate_key().decode())
            store = PostgresStore(tmp_path / "pg-store", _storage_client=SimpleNamespace(bucket=lambda _: None))
            application = FastAPI()
            @application.middleware("http")
            async def test_identity(request, call_next):
                request.state.identity = {"email": "postgres-test@example.test"}
                return await call_next(request)
            mapping_tables.register_mapping_tables(application, store)
            # Restart-safe CREATE TABLE IF NOT EXISTS, with no lost existing settings.
            store.set("fine_rules_config", {"buyer_name": "Before", "version": "pre-upload"})
            mapping_tables.register_mapping_tables(FastAPI(), store)
            with TestClient(application) as client:
                pending = upload(client, "unused").json()["upload_id"]
                assert activate(client, "unused", pending).status_code == 200
                assert client.get(BASE + "/active/workbook").status_code == 200
                baseline = next(v for v in client.get(BASE).json()["versions"] if v["source"] == "snapshot")
                assert client.post(BASE + f"/versions/{baseline['id']}/rollback").status_code == 200
                assert store.get("fine_rules_config") == {"buyer_name": "Before", "version": "pre-upload"}
        finally:
            subprocess.run([pg_ctl, "-D", data, "-m", "immediate", "-w", "stop"], check=True, capture_output=True)
