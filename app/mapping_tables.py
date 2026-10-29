"""Admin workbook workflow. Additive SQL shared by SQLite and PostgreSQL.

Workbook bytes are base64 TEXT so the schema and transactions are identical on
both stores. Only private storage contains cells; reports/audits carry counts.
"""
import base64
import io
import json
import re
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile

from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from .authorization import ADMIN, access
from .fine_rules import RulesConfig
from .fine_rules_tables import load
from .users import now_iso

MAX_BYTES = 8_000_000
MAX_EXPANDED_BYTES = 50_000_000
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TABLE_FIELDS = ("vat_codes", "location_master", "supplier_sites")
SCHEMA = """CREATE TABLE IF NOT EXISTS mapping_table_versions (
    id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL,
    metadata TEXT NOT NULL, tables_json TEXT NOT NULL, workbook TEXT NOT NULL)"""


def read_upload(filename, content):
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(415, "Upload an .xlsx workbook; macro-enabled files are not accepted")
    if len(content) > MAX_BYTES:
        raise HTTPException(413, "Mapping workbooks must be 8 MB or smaller")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise HTTPException(413, "Workbook expands beyond the supported size")
            names = [entry.filename.lower() for entry in entries]
            if len(names) != len(set(names)) or any(entry.flag_bits & 1 for entry in entries):
                raise HTTPException(400, "Workbook archive is invalid or encrypted")
            types = archive.read("[Content_Types].xml").lower()
            if any(token in types for token in (b"macroenabled", b"vbaproject", b"macrosheet")) or any(
                "vbaproject" in name or "macrosheet" in name for name in names
            ):
                raise HTTPException(415, "Macro-enabled workbooks are not accepted, including renamed files")
            if b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml" not in types:
                raise HTTPException(415, "Upload a standard .xlsx workbook")
        with warnings.catch_warnings():
            # openpyxl warnings can quote malformed cell contents. Reports use fixed reasons only.
            warnings.simplefilter("ignore")
            return load(io.BytesIO(content))
    except HTTPException:
        raise
    except (BadZipFile, KeyError):
        raise HTTPException(400, "The file is not a readable .xlsx workbook") from None
    except Exception:
        # Parser exceptions can contain cell values, XML snippets or sheet titles.
        raise HTTPException(400, "Workbook cannot be read; check Excel format and the 25000-row / 64-column limits") from None


def safe_filename(filename):
    name = re.split(r"[/\\]", filename)[-1]
    return "".join(c for c in name if c.isprintable())[:180] or "mapping-tables.xlsx"


def snapshot(config):
    # Preserve absent fields as absent when restoring a pre-upload configuration.
    return {key: config[key] for key in (*TABLE_FIELDS, "version") if key in config}


def merged_config(current, tables):
    return {**{key: value for key, value in current.items() if key not in (*TABLE_FIELDS, "version")}, **tables}


def checked_config(current, tables):
    config = merged_config(current, tables)
    try:
        RulesConfig.from_dict(config)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(409, "Existing rules configuration is incompatible; correct it before activating mappings") from None
    return config


def register_mapping_tables(app, store):
    with store.connection(True) as c:
        c.execute(SCHEMA)

    def get_record(c, record_id, state):
        row = c.execute("SELECT * FROM mapping_table_versions WHERE id=? AND state=?", (record_id, state)).fetchone()
        if row is None:
            raise HTTPException(404, "Mapping upload or version not found")
        return row

    def current_version(c):
        active = store.get("mapping_tables_active", {}, c)
        row = c.execute("SELECT tables_json FROM mapping_table_versions WHERE id=? AND state='version'",
                        (active.get("id", ""),)).fetchone()
        if row and json.loads(row[0]) == snapshot(store.get("fine_rules_config", {}, c)):
            return active
        return None

    def insert(c, record_id, state, metadata, tables, workbook=""):
        c.execute("INSERT INTO mapping_table_versions (id,state,created_at,metadata,tables_json,workbook) VALUES (?,?,?,?,?,?)",
                  (record_id, state, metadata["created_at"], json.dumps(metadata), json.dumps(tables), workbook))

    def preserve_previous(c, who):
        if current_version(c):
            return
        # Also preserve changes made through the older config API, so the next upload can be undone.
        previous = snapshot(store.get("fine_rules_config", {}, c))
        record_id = uuid.uuid4().hex
        metadata = {"id": record_id, "created_at": now_iso(), "created_by": who,
                    "filename": "Existing configuration", "has_workbook": False, "report": None,
                    "source": "snapshot", "version": previous.get("version", "unconfigured")}
        insert(c, record_id, "version", metadata, previous)

    @app.get("/api/mapping-tables")
    @access(ADMIN)
    def history():
        with store.connection() as c:
            versions = [json.loads(row[0]) for row in c.execute(
                "SELECT metadata FROM mapping_table_versions WHERE state='version' ORDER BY created_at DESC,id DESC")]
            return {"active": current_version(c), "versions": versions, "max_bytes": MAX_BYTES}

    @app.post("/api/mapping-tables/validate")
    @access(ADMIN)
    def validate_upload(request: Request, file: UploadFile = File(...)):
        try:
            content = file.file.read(MAX_BYTES + 1)
        finally:
            file.file.close()
        tables, report = read_upload(file.filename or "", content)
        result = {"upload_id": None, "filename": safe_filename(file.filename or ""), "report": report}
        if report["valid"]:
            upload_id = uuid.uuid4().hex
            metadata = {"id": upload_id, "created_at": now_iso(), "created_by": request.state.identity["email"],
                        "filename": result["filename"], "report": report, "has_workbook": True, "source": "upload"}
            with store.connection(True) as c:
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
                c.execute("DELETE FROM mapping_table_versions WHERE state='validated' AND created_at<?", (cutoff,))
                count = c.execute("SELECT COUNT(*) FROM mapping_table_versions WHERE state='validated'").fetchone()[0]
                if count >= 20:
                    raise HTTPException(409, "There are 20 pending uploads; activate one or wait for their 24-hour expiry")
                insert(c, upload_id, "validated", metadata, tables, base64.b64encode(content).decode("ascii"))
            result["upload_id"] = upload_id
        # Never audit filenames, unknown headings, cell values or parser exception text.
        store.audit("mapping_tables_validated", {"valid": report["valid"], "sheets": report["sheets"],
                                                 "flagged_count": len(report["flagged"])})
        return result

    @app.post("/api/mapping-tables/uploads/{upload_id}/activate")
    @access(ADMIN)
    def activate(upload_id: str, request: Request):
        who = request.state.identity["email"]
        with store.connection(True) as c:
            row = get_record(c, upload_id, "validated")
            if datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"]) >= timedelta(hours=24):
                raise HTTPException(409, "Upload expired; upload the workbook again")
            metadata = json.loads(row["metadata"])
            stamp = now_iso()
            tables = {**json.loads(row["tables_json"]), "version": f"mapping-{stamp}-{upload_id[:8]}"}
            config = checked_config(store.get("fine_rules_config", {}, c), tables)
            preserve_previous(c, who)
            metadata.update(uploaded_at=metadata["created_at"], uploaded_by=metadata["created_by"],
                            created_at=stamp, created_by=who, version=tables["version"])
            c.execute("UPDATE mapping_table_versions SET state='version',created_at=?,metadata=?,tables_json=? WHERE id=?",
                      (stamp, json.dumps(metadata), json.dumps(tables), upload_id))
            active = {"id": upload_id, "activated_at": stamp, "activated_by": who}
            store.set("fine_rules_config", config, c)
            store.set("mapping_tables_active", active, c)
            store.audit("mapping_tables_activated", {"version_id": upload_id, "sheets": metadata["report"]["sheets"]}, c)
        return {"active": active, "version": metadata}

    @app.post("/api/mapping-tables/versions/{version_id}/rollback")
    @access(ADMIN)
    def rollback(version_id: str, request: Request):
        with store.connection(True) as c:
            row = get_record(c, version_id, "version")
            config = checked_config(store.get("fine_rules_config", {}, c), json.loads(row["tables_json"]))
            preserve_previous(c, request.state.identity["email"])
            active = {"id": version_id, "activated_at": now_iso(), "activated_by": request.state.identity["email"]}
            store.set("fine_rules_config", config, c)
            store.set("mapping_tables_active", active, c)
            store.audit("mapping_tables_rolled_back", {"version_id": version_id}, c)
        return {"active": active}

    @app.get("/api/mapping-tables/active/workbook")
    @access(ADMIN)
    def download_active():
        with store.connection() as c:
            active = current_version(c)
            if not active:
                raise HTTPException(404, "The active configuration has no uploaded workbook")
            row = get_record(c, active["id"], "version")
            if not row["workbook"]:
                raise HTTPException(404, "This configuration was saved without a workbook")
            filename = json.loads(row["metadata"])["filename"]
            return Response(base64.b64decode(row["workbook"]), media_type=MIME,
                            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""),
                                     "Cache-Control": "no-store"})
