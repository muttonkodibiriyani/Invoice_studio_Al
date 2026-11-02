"""Independent item, order and location blocks of the October item master.

Catalog versions contain private compressed source records. Activation and rollback
replace only lookup tables; VAT, location-market and supplier configuration survive.
"""
import base64
import gzip
import hashlib
import io
import json
import math
import re
import uuid
import warnings
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from zipfile import ZipFile

from fastapi import File, HTTPException, Request, UploadFile

from .authorization import ADMIN, access
from .reference_lookup import _configured_columns, _record, _terms

MAX_BYTES = 16_000_000
MAX_EXPANDED_BYTES = 300_000_000
MAX_ROWS = 150_000
MAX_DIAGNOSTICS = 200
HEADERS = ("ITEM", "BARCODE", "ITEM_PARENT", "ITEM_DESC", "VPN", "BRAND", "SUPPLIER_NAME",
           "SUPPLIER SITE NAME", "SUPPLIER SITE", "LOCATION NUMBER", "LOCATION NAME", "LOCATION NAME 2",
           "MANUAL ORDER NO", "RMS_ORDER_NO", "BARCODE_1", "RMS_ITEM_ID", "QTY_RECEIVED")
COLUMNS = {"item": {"gtin": ["BARCODE"], "internal_item": ["ITEM_PARENT"], "sku": ["VPN"],
                    "supplier": ["SUPPLIER_NAME", "SUPPLIER SITE NAME"], "site": ["SUPPLIER SITE"],
                    "description": ["ITEM_DESC"]},
           "po": {"gtin": ["BARCODE_1"], "internal_item": ["RMS_ITEM_ID"],
                  "po": ["RMS_ORDER_NO", "MANUAL ORDER NO"]}}
SCHEMA = """CREATE TABLE IF NOT EXISTS item_master_versions(
    id TEXT PRIMARY KEY,state TEXT NOT NULL,metadata TEXT NOT NULL,archive TEXT NOT NULL)"""


def scalar(cell):
    value = cell.value
    if value is None:
        return ""
    if cell.data_type in ("f", "e", "d") or isinstance(value, (bool, date, datetime)):
        raise ValueError("formula, error, date or boolean in a literal field")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or abs(value) >= 10 ** 15:
            raise ValueError("numeric identifier may have lost precision")
        value = int(value) if isinstance(value, float) and value.is_integer() else value
    text = str(value).strip()
    if len(text) > 2000:
        raise ValueError("cell exceeds supported length")
    # Literal text retains leading zeros; numeric cells with an explicit zero mask retain theirs too.
    if isinstance(value, int) and re.fullmatch(r"0{2,}", cell.number_format or ""):
        text = text.zfill(len(cell.number_format))
    return text


def inspect_workbook(content):
    """Return canonical block records and a bounded, cell-value-free full-file report."""
    import openpyxl

    if len(content) > MAX_BYTES:
        raise HTTPException(413, "Item masters must be 16 MB or smaller")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
                raise HTTPException(413, "Item master expands beyond the supported size")
            names = [e.filename.lower() for e in entries]
            if len(names) != len(set(names)) or any(e.flag_bits & 1 for e in entries):
                raise HTTPException(400, "Workbook archive is invalid or encrypted")
            types = archive.read("[Content_Types].xml").lower()
            if any(x in types for x in (b"macroenabled", b"vbaproject", b"macrosheet")) or any("vbaproject" in n for n in names):
                raise HTTPException(415, "Macro-enabled workbooks are not accepted")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        if len(book.worksheets) != 1:
            raise ValueError("one sheet required")
        sheet = book.worksheets[0]
        sheet.reset_dimensions()
        rows = sheet.iter_rows()
        header = [str(c.value or "").strip().upper() for c in next(rows, ())]
        if len(header) != len(HEADERS) or len(set(header)) != len(header) or set(header) != set(HEADERS):
            raise ValueError("required item master headings missing or duplicated")
        source_hash = hashlib.sha256(content).hexdigest()
        records, locations, diagnostics, reasons = [], [], [], Counter()
        distinct = {c: set() for c in HEADERS}
        filled, types, duplicate_rows, duplicate_po = Counter(), defaultdict(Counter), 0, 0
        physical, po_seen = set(), set()
        site_vpns, parent_items, manual_orders = defaultdict(set), defaultdict(set), defaultdict(set)
        block_counts, row_count = Counter(), 0
        def flag(number, block, reason):
            reasons[reason] += 1
            if len(diagnostics) < MAX_DIAGNOSTICS:
                diagnostics.append({"row": number, "block": block, "reason": reason})
        try:
            for number, cells in enumerate(rows, 2):
                if number > MAX_ROWS + 1 or len(cells) > len(HEADERS):
                    raise ValueError("item master row or column limit exceeded")
                if all(c.value is None for c in cells):
                    continue
                row_count += 1
                data, bad = {}, set()
                for name, cell in zip(header, cells):
                    types[name][cell.data_type] += 1
                    try:
                        data[name] = scalar(cell)
                    except ValueError:
                        data[name] = ""
                        bad.add(name)
                        flag(number, "cell", "invalid literal field: " + name)
                    if data[name]:
                        filled[name] += 1
                        distinct[name].add(data[name])
                data = {h: data.get(h, "") for h in HEADERS}
                fingerprint = tuple(data.values())
                duplicate_rows += fingerprint in physical
                physical.add(fingerprint)
                item = {h: data[h] for h in HEADERS[:9]}
                flags = []
                if bad & set(HEADERS[:9]):
                    flags.append("quarantined invalid item literal")
                if not item["ITEM_PARENT"] or not item["SUPPLIER SITE"]:
                    flags.append("quarantined missing item parent or supplier site")
                    flag(number, "item", "missing item parent or supplier site")
                if not item["VPN"]:
                    flag(number, "item", "missing VPN; barcode fallback only")
                records.append({"kind": "item", "source_hash": source_hash, "source_sheet": sheet.title,
                                "source_row": number, "keys": [], "data": item, "flags": flags})
                block_counts["item"] += 1
                if item["SUPPLIER SITE"] and item["VPN"]:
                    site_vpns[(item["SUPPLIER SITE"], item["VPN"])].add(item["ITEM_PARENT"])
                parent_items[item["ITEM_PARENT"]].add(item["ITEM"])
                loc = {h: data[h] for h in HEADERS[9:12]}
                if any(loc.values()):
                    if bad & set(HEADERS[9:12]) or not loc["LOCATION NUMBER"]:
                        flag(number, "location", "quarantined invalid location directory row")
                    else:
                        locations.append({"source_hash": source_hash, "source_sheet": sheet.title,
                                          "source_row": number, "data": loc})
                po = {h: data[h] for h in HEADERS[12:]}
                if any(po.values()):
                    flags = ["receipt identity unavailable; quantity is not an available balance"]
                    if bad & set(HEADERS[12:]) or not all(po[h] for h in ("RMS_ORDER_NO", "BARCODE_1", "RMS_ITEM_ID")):
                        flags.append("quarantined missing or invalid order bridge")
                        flag(number, "po", "missing or invalid order bridge")
                    key = tuple(po.values())
                    if key in po_seen:
                        flags.append("repeated PO tuple; never sum receipts")
                        duplicate_po += 1
                    po_seen.add(key)
                    if po["MANUAL ORDER NO"]:
                        manual_orders[po["MANUAL ORDER NO"]].add(po["RMS_ORDER_NO"])
                    records.append({"kind": "po", "source_hash": source_hash, "source_sheet": sheet.title,
                                    "source_row": number, "keys": [], "data": po, "flags": flags})
                    block_counts["po"] += 1
        finally:
            book.close()
        if not row_count:
            raise ValueError("empty master")
        report = {"valid": True, "source_sha256": source_hash, "bytes": len(content), "rows": row_count,
                  "blocks": dict(block_counts), "locations": len(locations), "duplicate_rows": duplicate_rows,
                  "duplicate_po_tuples": duplicate_po, "ambiguous_site_vpn": sum(len(v) > 1 for v in site_vpns.values()),
                  "parents_with_multiple_items": sum(len(v) > 1 for v in parent_items.values()),
                  "ambiguous_manual_orders": sum(len(v) > 1 for v in manual_orders.values()),
                  "columns": {h: {"filled": filled[h], "blank": row_count - filled[h], "distinct": len(distinct[h]),
                                   "types": dict(types[h])} for h in HEADERS},
                  "nonblank_rms_orders": filled["RMS_ORDER_NO"], "nonblank_po_parents": filled["RMS_ITEM_ID"],
                  "diagnostic_counts": dict(reasons), "diagnostics": diagnostics,
                  "diagnostic_total": sum(reasons.values()), "diagnostics_truncated": sum(reasons.values()) > len(diagnostics)}
        return records, {"columns": COLUMNS, "locations": locations, "report": report}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Item master cannot be read; check the single-sheet 17-column schema and 150000-row limit") from None


def pack(records):
    return base64.b64encode(gzip.compress(json.dumps(records, separators=(",", ":")).encode())).decode()


def unpack(archive):
    return json.loads(gzip.decompress(base64.b64decode(archive)))


def fingerprint(c):
    return hashlib.sha256(json.dumps([r[0] for r in c.execute("SELECT payload FROM lookup_sources ORDER BY id")]).encode()).hexdigest()


def install(store, c, records, source):
    """One serialized transaction; any parse/index/write failure rolls back the previous live catalog."""
    configured = _configured_columns({"columns": source.get("master_catalog", {}).get("columns", {})})
    c.execute("DROP INDEX IF EXISTS lookup_rows_source_row")
    c.execute("CREATE UNIQUE INDEX lookup_rows_source_row ON lookup_rows(source_hash,sheet,row_number,kind)")
    c.execute("DELETE FROM lookup_terms")
    c.execute("DELETE FROM lookup_rows")
    c.execute("DELETE FROM lookup_sources")
    role_cache = {}
    for offset in range(0, len(records), 1000):
        prepared = []
        for raw in records[offset:offset + 1000]:
            if "candidate_fields" in raw:
                prepared.append((raw["id"], raw["kind"], raw["source_hash"], raw["source_sheet"], raw["source_row"],
                                 json.dumps(raw), _terms(raw, raw["candidate_fields"])))
            else:
                prepared.append(_record(raw, configured, role_cache))
        c.executemany("INSERT INTO lookup_rows(id,kind,source_hash,sheet,row_number,payload) VALUES (?,?,?,?,?,?)",
                      [r[:6] for r in prepared])
        c.executemany("INSERT INTO lookup_terms(kind,term,row_id) VALUES (?,?,?)",
                      [(r[1], term, r[0]) for r in prepared for term in r[6]])
    for record in source.get("_restore_sources", [source]):
        c.execute("INSERT INTO lookup_sources(id,payload) VALUES (?,?)", (record["id"], json.dumps(record)))


def register_item_master(app, store):
    with store.connection(True) as c:
        c.execute(SCHEMA)

    @app.get("/api/item-master")
    @access(ADMIN)
    def history():
        with store.connection() as c:
            return {"active": store.get("item_master_active", None, c), "signature": fingerprint(c),
                    "versions": [json.loads(r[0]) for r in c.execute("SELECT metadata FROM item_master_versions WHERE state='version' ORDER BY id")]}

    @app.post("/api/item-master/validate")
    @access(ADMIN)
    def validate(request: Request, file: UploadFile = File(...)):
        try:
            content = file.file.read(MAX_BYTES + 1)
        finally:
            file.file.close()
        if not (file.filename or "").lower().endswith(".xlsx"):
            raise HTTPException(415, "Upload a standard .xlsx item master")
        records, metadata = inspect_workbook(content)
        version = uuid.uuid4().hex
        archive = pack(records)
        with store.connection(True) as c:
            if c.execute("SELECT COUNT(*) FROM item_master_versions WHERE state='validated'").fetchone()[0] >= 3:
                raise HTTPException(409, "Three item masters await activation; activate or delete a pending upload")
            metadata.update(id=version, created_at=datetime.now(timezone.utc).isoformat(),
                            created_by=request.state.identity["email"], base_signature=fingerprint(c))
            c.execute("INSERT INTO item_master_versions VALUES (?,?,?,?)", (version, "validated", json.dumps(metadata), archive))
            store.audit("item_master_validated", {"version_id": version, "rows": metadata["report"]["rows"],
                                                 "diagnostic_total": metadata["report"]["diagnostic_total"]}, c)
        return {"upload_id": version, "base_signature": metadata["base_signature"], "report": metadata["report"]}

    def activate_version(c, version, who, rollback=False, expected=None):
        row = c.execute("SELECT state,metadata,archive FROM item_master_versions WHERE id=?", (version,)).fetchone()
        if not row or row[0] != ("version" if rollback else "validated"):
            raise HTTPException(404, "Item master upload or version not found")
        metadata = json.loads(row[1])
        current = fingerprint(c)
        if (expected if rollback else metadata["base_signature"]) != current:
            raise HTTPException(409, "Catalog changed; validate again or refresh its signature before rollback")
        # Snapshot any existing catalog, including one imported by the older catalog tooling.
        if not store.get("item_master_active", None, c) or store.get("item_master_active_signature", None, c) != current:
            raw = []
            for r in c.execute("SELECT payload FROM lookup_rows ORDER BY id"):
                payload = json.loads(r[0])
                raw.append(payload)
            sources = [json.loads(r[0]) for r in c.execute("SELECT payload FROM lookup_sources ORDER BY id")]
            snap_id = uuid.uuid4().hex
            snapshot = {"id": snap_id, "created_at": datetime.now(timezone.utc).isoformat(), "source": "snapshot",
                        "restore_sources": sources}
            c.execute("INSERT INTO item_master_versions VALUES (?,?,?,?)", (snap_id, "version", json.dumps(snapshot), pack(raw)))
        source = {"id": "master:" + version, "version": version, "source_hashes": [metadata["report"]["source_sha256"]],
                  "counts": {**metadata["report"]["blocks"], "total": sum(metadata["report"]["blocks"].values())},
                  "master_catalog": {k: metadata[k] for k in ("columns", "locations")}} if "report" in metadata else {
                      "id": "snapshot:" + version, "_restore_sources": metadata["restore_sources"]}
        records = unpack(row[2])
        if not rollback:
            # Sparse master PO blocks supplement richer existing GRN evidence, never erase it.
            old_sources = [json.loads(r[0]) for r in c.execute("SELECT payload FROM lookup_sources ORDER BY id")]
            prior_master_hashes = {h for src in old_sources if src.get("master_catalog") for h in src.get("source_hashes", [])}
            richer = [json.loads(r[0]) for r in c.execute("SELECT payload FROM lookup_rows WHERE kind='po'")]
            richer = [r for r in richer if r["source_hash"] not in prior_master_hashes]
            if richer:
                records.extend(richer)
                source["_restore_sources"] = [src for src in old_sources if not src.get("master_catalog")] + [dict(source)]
        if rollback and metadata.get("activated_sources") is not None:
            source = {"id": "restore:" + version, "master_catalog": {"columns": metadata.get("columns", {})},
                      "_restore_sources": metadata["activated_sources"]}
        install(store, c, records, source)
        metadata["activated_sources"] = source.get("_restore_sources", [source])
        c.execute("UPDATE item_master_versions SET state='version',metadata=?,archive=? WHERE id=?",
                  (json.dumps(metadata), pack(records), version))
        store.set("item_master_active", version, c)
        store.set("item_master_active_signature", fingerprint(c), c)
        store.audit("item_master_rolled_back" if rollback else "item_master_activated", {"version_id": version, "actor": who}, c)
        return {"active": version, "signature": fingerprint(c)}

    @app.post("/api/item-master/uploads/{version}/activate")
    @access(ADMIN)
    def activate(version: str, request: Request):
        with store.connection(True) as c:
            return activate_version(c, version, request.state.identity["email"])

    @app.post("/api/item-master/versions/{version}/rollback")
    @access(ADMIN)
    def rollback(version: str, request: Request, expected_signature: str):
        with store.connection(True) as c:
            return activate_version(c, version, request.state.identity["email"], True, expected_signature)

    @app.delete("/api/item-master/uploads/{version}")
    @access(ADMIN)
    def discard(version: str):
        with store.connection(True) as c:
            c.execute("DELETE FROM item_master_versions WHERE id=? AND state='validated'", (version,))
        return {"discarded": True}
