"""Read-only, source-proven reference discovery; never an approval source."""

import base64
import gzip
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


KINDS = ("item", "po")
FIELD_ROLES = (
    "sku", "gtin", "internal_item", "identity", "uom", "pack", "description",
    "supplier", "site", "po",
)
MAX_LINE_BYTES = 2_000_000
MAX_RECORDS = 5_000_000


def normalize_name(value):
    """Normalize human names without turning them into canonical business data."""
    if not isinstance(value, str):
        value = str(value)
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in value).split())


def normalize_identifier(value):
    return normalize_name(value).replace(" ", "")


def _header_roles(header, kind, configured):
    normalized = normalize_name(header)
    roles = []
    declared = configured.get(kind, {})
    for role, headers in declared.items():
        if normalized in headers:
            roles.append(role)
    # A manifest mapping is the source contract. Do not add semantic roles
    # from column-name guesses (for example, this catalog's ITEM is a barcode,
    # while ITEM_PARENT is its internal item identifier).
    if declared:
        return list(dict.fromkeys(roles))
    words = set(normalized.split())
    if normalized in {"item", "item id", "item code", "item number", "rms item id"}:
        roles.append("internal_item")
    if roles:
        return list(dict.fromkeys(roles))
    if any(token in words for token in ("gtin", "upc", "ean", "barcode")):
        roles.append("gtin")
    if normalized in {"uom", "unit", "unit of measure", "base unit", "order unit"}:
        roles.append("uom")
    if "pack" in words and not words.intersection({"description", "name"}):
        roles.append("pack")
    if words.intersection({"supplier", "vendor"}):
        roles.append("supplier")
    if words.intersection({"site", "plant", "warehouse", "branch"}) or normalized in {
        "location", "location code", "location name", "store", "store code", "store name"
    }:
        roles.append("site")
    if normalized in {"po", "po no", "po number", "purchase order", "purchase order no",
                      "purchase order number", "purchasing document"}:
        roles.append("po")
    if "description" in words or normalized in {
        "name", "item name", "product name", "material name", "short text", "article name"
    }:
        roles.append("description")
    if normalized in {"sku", "item", "item code", "item id", "item number", "material",
                      "material code", "material id", "material number", "product code",
                      "product id", "article", "article code", "article number", "internal item"}:
        roles.append("sku")
    return list(dict.fromkeys(roles))


def _cell_values(value):
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, float) and not math.isfinite(item):
            continue
        if not isinstance(item, (str, int, float)):
            continue
        text = str(item).strip()
        if text and len(text) <= 2_000 and text not in result:
            result.append(text)
    return result


def _candidate_fields(data, kind, configured, role_cache=None):
    fields = {role: [] for role in FIELD_ROLES}
    sources = {role: [] for role in FIELD_ROLES}
    for column, value in data.items():
        cache_key = (kind, column)
        roles = role_cache.get(cache_key) if role_cache is not None else None
        if roles is None:
            roles = _header_roles(column, kind, configured)
            if role_cache is not None:
                role_cache[cache_key] = roles
        if not roles:
            continue
        for text in _cell_values(value):
            for role in roles:
                if text not in fields[role]:
                    fields[role].append(text)
                sources[role].append({"column": column, "value": text})
    return fields, sources


def _terms(record, fields):
    terms = set()
    # ``keys`` is preserved provenance supplied by the converter, but it can
    # contain descriptions and context values. Only typed source columns are
    # safe to label and rank as identifiers.
    for value in [*fields["sku"], *fields["gtin"], *fields["internal_item"], *fields["po"]]:
        normalized = normalize_identifier(value)
        if 2 <= len(normalized) <= 300:
            terms.add("i:" + normalized)
    for role in ("sku", "gtin", "internal_item"):
        for value in fields[role]:
            normalized = normalize_identifier(value)
            if 2 <= len(normalized) <= 300:
                terms.add(f"x:{role}:{normalized}")
    for value in fields["description"]:
        normalized = normalize_name(value)
        if 2 <= len(normalized) <= 1_000:
            terms.add("n:" + normalized)
            terms.update("t:" + token for token in normalized.split() if 2 <= len(token) <= 100)
    for role in ("supplier", "site", "po"):
        for value in fields[role]:
            normalized = normalize_identifier(value) if role == "po" else normalize_name(value)
            if 1 <= len(normalized) <= 500:
                terms.add(f"f:{role}:{normalized}")
    return sorted(terms)


def _configured_columns(manifest):
    raw = manifest.get("columns", {})
    if not isinstance(raw, dict) or set(raw) - set(KINDS):
        raise ValueError("Manifest columns must be grouped by item or po")
    configured = {kind: {} for kind in KINDS}
    for kind, mapping in raw.items():
        if not isinstance(mapping, dict) or set(mapping) - set(FIELD_ROLES):
            raise ValueError(f"Manifest {kind} columns include an unknown field role")
        for role, headers in mapping.items():
            if isinstance(headers, str):
                headers = [headers]
            if not isinstance(headers, list) or any(not isinstance(x, str) for x in headers):
                raise ValueError(f"Manifest {kind}.{role} columns must be a string list")
            normalized = {normalize_name(header) for header in headers}
            if "" in normalized:
                raise ValueError(f"Manifest {kind}.{role} includes a blank column")
            configured[kind][role] = normalized
    return configured


def _expected_manifest(manifest):
    if not isinstance(manifest, dict) or not ("version" in manifest or "manifest_version" in manifest):
        raise ValueError("Manifest must be a versioned JSON object")
    counts = manifest.get("expected_counts", manifest.get("counts"))
    if not isinstance(counts, dict) or not all(key in counts for key in (*KINDS, "total")):
        raise ValueError("Manifest must declare expected item, po and total counts")
    expected = {}
    for key in (*KINDS, "total"):
        value = counts[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Manifest counts must be non-negative integers")
        expected[key] = value
    if expected["total"] != expected["item"] + expected["po"] or expected["total"] > MAX_RECORDS:
        raise ValueError("Manifest total must equal item plus po counts within the import limit")
    declared = None
    if isinstance(manifest.get("sources"), list) and manifest["sources"]:
        declared = [item.get("source_hash", item.get("hash")) for item in manifest["sources"]
                    if isinstance(item, dict)]
    if declared is None:
        declared = manifest.get("source_hashes")
        if isinstance(declared, dict):
            declared = list(declared.values())
    if not isinstance(declared, list) or not declared:
        raise ValueError("Manifest must declare source_hashes or sources")
    hashes = set()
    for value in declared:
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("Manifest source hashes must be lowercase SHA-256 values")
        hashes.add(value)
    return expected, hashes, _configured_columns(manifest)


def _record(raw, configured, role_cache=None):
    required = {"kind", "source_hash", "source_sheet", "source_row", "keys", "data", "flags"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("Each lookup row must contain exactly the documented fields")
    kind = raw["kind"]
    if kind not in KINDS:
        raise ValueError("Lookup row kind must be item or po")
    source_hash = raw["source_hash"]
    if not isinstance(source_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", source_hash):
        raise ValueError("Lookup row source_hash must be a lowercase SHA-256 value")
    sheet = raw["source_sheet"]
    if not isinstance(sheet, str) or not 1 <= len(sheet) <= 300 or any(ord(c) < 0x20 for c in sheet):
        raise ValueError("Lookup row source_sheet is invalid")
    row_number = raw["source_row"]
    if isinstance(row_number, bool) or not isinstance(row_number, int) or not 1 <= row_number <= 100_000_000:
        raise ValueError("Lookup row source_row is invalid")
    keys = raw["keys"]
    if not isinstance(keys, list) or len(keys) > 100 or any(
        not isinstance(value, str) or not 1 <= len(value) <= 2_000 for value in keys
    ):
        raise ValueError("Lookup row keys must be a bounded string list")
    data = raw["data"]
    if not isinstance(data, dict) or len(data) > 500 or any(
        not isinstance(key, str) or not 1 <= len(key) <= 300 for key in data
    ):
        raise ValueError("Lookup row data must be a bounded original-column object")
    if any(not isinstance(value, (str, int, float, bool, type(None), list)) for value in data.values()):
        raise ValueError("Lookup row column values must be scalar values or scalar lists")
    for value in data.values():
        if isinstance(value, list) and (len(value) > 100 or any(
            not isinstance(item, (str, int, float, bool, type(None))) for item in value
        )):
            raise ValueError("Lookup row column lists are invalid")
    flags = raw["flags"]
    if not isinstance(flags, list) or len(flags) > 100 or any(
        not isinstance(value, str) or not 1 <= len(value) <= 500 for value in flags
    ):
        raise ValueError("Lookup row flags must be a bounded string list")
    fields, sources = _candidate_fields(data, kind, configured, role_cache)
    identifier = hashlib.sha256(
        (source_hash + "\x00" + sheet + "\x00" + str(row_number)).encode()
    ).hexdigest()
    payload = {"id": identifier, **raw, "candidate_fields": fields,
               "candidate_field_sources": sources, "approved_for_matching": False,
               "requires_confirmation": True}
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > MAX_LINE_BYTES:
        raise ValueError("Lookup row payload exceeds the size limit")
    return identifier, kind, source_hash, sheet, row_number, encoded, _terms(raw, fields)


def _like_prefix(value):
    """Build a literal SQL LIKE prefix for both SQLite and PostgreSQL."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _cursor_encode(signature, score, identifier):
    body = json.dumps([signature, int(score), identifier], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


def _cursor_decode(value, signature):
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = json.loads(raw)
        if (not isinstance(decoded, list) or len(decoded) != 3 or decoded[0] != signature
                or isinstance(decoded[1], bool) or not isinstance(decoded[1], int)
                or not isinstance(decoded[2], str) or not re.fullmatch(r"[a-f0-9]{64}", decoded[2])):
            raise ValueError()
        return decoded[1], decoded[2]
    except Exception:
        raise ValueError("Invalid or stale page cursor") from None



class ReferenceLookup:
    def __init__(self, store):
        self.store = store
        with store.connection() as connection:
            postgres = hasattr(connection, "connection") and hasattr(
                connection.connection, "cursor"
            )
            if postgres:
                existing = connection.execute(
                    """SELECT to_regclass('public.lookup_sources'),
                              to_regclass('public.lookup_rows'),
                              to_regclass('public.lookup_terms'),
                              to_regclass('public.lookup_rows_kind_id'),
                              to_regclass('public.lookup_rows_source_row')"""
                ).fetchone()
                # CREATE INDEX IF NOT EXISTS still takes a table lock in
                # PostgreSQL. Avoid startup DDL when the complete schema is
                # already present so an atomic catalog import cannot stall new
                # application instances.
                if existing is not None and all(value is not None for value in existing):
                    return
            connection.execute(
                "CREATE TABLE IF NOT EXISTS lookup_sources(id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS lookup_rows(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,source_hash TEXT NOT NULL,
                    sheet TEXT NOT NULL,row_number INTEGER NOT NULL,payload TEXT NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS lookup_terms(
                    kind TEXT NOT NULL,term TEXT NOT NULL,row_id TEXT NOT NULL,
                    PRIMARY KEY(kind,term,row_id))"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS lookup_rows_kind_id ON lookup_rows(kind,id)"
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS lookup_rows_source_row
                   ON lookup_rows(source_hash,sheet,row_number)"""
            )

    def summary(self):
        with self.store.connection() as connection:
            sources = [json.loads(row["payload"]) for row in connection.execute(
                "SELECT payload FROM lookup_sources ORDER BY id"
            )]
            counts = {kind: 0 for kind in KINDS}
            for row in connection.execute("SELECT kind,COUNT(*) AS count FROM lookup_rows GROUP BY kind"):
                counts[row["kind"]] = int(row["count"])
        counts["total"] = sum(counts.values())
        return {"approved_for_matching": False, "requires_confirmation": True,
                "counts": counts, "sources": sources,
                "notice": "Lookup evidence only. A person must choose a row; it never approves matching or export."}

    def search(self, kind, query, cursor="", limit=25, site="", supplier="", po=""):
        if kind not in KINDS:
            raise ValueError("Choose item or PO/GRN records")
        q = normalize_name(query)
        if not 2 <= len(q) <= 100:
            raise ValueError("Enter 2 to 100 characters: an item code or product description")
        if not 1 <= limit <= 50:
            raise ValueError("Page size must be between 1 and 50")
        filters = {
            "site": normalize_name(site) if site else "",
            "supplier": normalize_name(supplier) if supplier else "",
            "po": normalize_identifier(po) if po else "",
        }
        if any(len(value) > 300 for value in filters.values()):
            raise ValueError("Lookup filters must be 300 characters or fewer")
        signature = hashlib.sha256(json.dumps(
            [kind, q, filters], sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()[:16]
        cursor_score, cursor_id = _cursor_decode(cursor, signature) if cursor else (None, None)
        identifier_term = "i:" + normalize_identifier(query)
        name_term = "n:" + q
        tokens = list(dict.fromkeys(q.split()))
        token_terms = ["t:" + token for token in tokens]

        score_parts = ["MAX(CASE WHEN t.term=? THEN 100000 WHEN t.term=? THEN 90000 ELSE 0 END)"]
        score_params = [identifier_term, name_term]
        token_placeholders = ",".join("?" for _ in token_terms)
        score_parts.append(
            f"SUM(CASE WHEN t.term IN ({token_placeholders}) THEN 1000 ELSE 500 END)"
        )
        score_params.extend(token_terms)
        match_sql = ["t.term=?", "t.term=?"]
        match_params = [identifier_term, name_term]
        for term in token_terms:
            # PostgreSQL locale collations do not guarantee that ``t:word`` is
            # less than the synthetic upper bound ``t;``. LIKE with an explicit
            # escape keeps prefix matching correct in PostgreSQL and SQLite.
            match_sql.append("t.term LIKE ? ESCAPE '\\'")
            match_params.append(_like_prefix(term))
        constraints = []
        constraint_params = []
        for role, value in filters.items():
            if value:
                if role == "po" and kind == "item":
                    constraints.append(
                        """EXISTS (
                           SELECT 1 FROM lookup_terms item_key
                           JOIN lookup_terms po_key ON po_key.kind='po' AND po_key.term=item_key.term
                           JOIN lookup_terms po_filter ON po_filter.kind='po'
                                AND po_filter.row_id=po_key.row_id AND po_filter.term=?
                           WHERE item_key.kind='item' AND item_key.row_id=t.row_id
                                AND item_key.term LIKE ? ESCAPE '\\')"""
                    )
                    constraint_params.extend((f"f:po:{value}", _like_prefix("x:")))
                else:
                    constraints.append(
                        "EXISTS (SELECT 1 FROM lookup_terms ft WHERE ft.kind=t.kind "
                        "AND ft.row_id=t.row_id AND ft.term=?)"
                    )
                    constraint_params.append(f"f:{role}:{value}")
        inner = (
            "SELECT t.row_id AS id,(" + "+".join(score_parts) + ") AS score "
            "FROM lookup_terms t "
            "WHERE t.kind=? AND (" + " OR ".join(match_sql) + ")"
        )
        if constraints:
            inner += " AND " + " AND ".join(constraints)
        inner += " GROUP BY t.row_id"
        ranked = "SELECT id,score FROM (" + inner + ") scored"
        params = [*score_params, kind, *match_params, *constraint_params]
        if cursor_score is not None:
            ranked += " WHERE score<? OR (score=? AND id>?)"
            params.extend((cursor_score, cursor_score, cursor_id))
        ranked += " ORDER BY score DESC,id ASC LIMIT ?"
        params.append(limit + 1)
        sql = (
            "SELECT ranked.id,r.payload,ranked.score FROM (" + ranked + ") ranked "
            "JOIN lookup_rows r ON r.id=ranked.id "
            "ORDER BY ranked.score DESC,ranked.id ASC"
        )
        with self.store.connection() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        more = len(rows) > limit
        selected = rows[:limit]
        records = [self._public_candidate(json.loads(row["payload"]), q, int(row["score"]), filters)
                   for row in selected]
        next_cursor = _cursor_encode(signature, int(selected[-1]["score"]), selected[-1]["id"]) \
            if more and selected else None
        return {"kind": kind, "query": query, "normalized_query": q,
                "filters": filters, "approved_for_matching": False,
                "requires_confirmation": True, "records": records,
                "next_cursor": next_cursor,
                "notice": "Candidates are ranked lookup evidence. Select a specific source row before copying any field."}

    @staticmethod
    def _public_candidate(record, query, database_score, filters):
        query_id = normalize_identifier(query)
        tokens = query.split()
        identifiers = {normalize_identifier(value) for value in [
            *record["candidate_fields"]["sku"], *record["candidate_fields"]["gtin"],
            *record["candidate_fields"]["internal_item"], *record["candidate_fields"]["po"]
        ]}
        descriptions = [normalize_name(value) for value in record["candidate_fields"]["description"]]
        description_tokens = {token for description in descriptions for token in description.split()}
        basis = []
        matched = []
        if query_id in identifiers:
            basis.append("exact_identifier");matched.append(query_id);score = 1.0
        elif query in descriptions:
            basis.append("exact_description");matched.append(query);score = 0.95
        else:
            weights = []
            for token in tokens:
                if token in description_tokens:
                    if "description_token" not in basis:basis.append("description_token")
                    matched.append(token);weights.append(1.0)
                elif any(value.startswith(token) for value in description_tokens):
                    if "description_prefix" not in basis:basis.append("description_prefix")
                    matched.append(token);weights.append(0.7)
                else:
                    weights.append(0.0)
            score = round(0.5 + 0.39 * (sum(weights) / max(1, len(tokens))), 3)
        context_basis = [f"exact_{role}_context" for role, value in filters.items() if value]
        basis.extend(context_basis)
        record["match"] = {"basis": basis, "score": score,
                           "matched_terms": list(dict.fromkeys(matched)),
                           "matched_context": {key: value for key, value in filters.items() if value},
                           "rank": database_score}
        return record

    def import_archive(self, archive_path, manifest_path, batch_size=2_000):
        """Atomically replace lookup-only tables from a validated gzip NDJSON export."""
        archive_path = Path(archive_path)
        manifest_path = Path(manifest_path)
        if manifest_path.stat().st_size > 1_000_000:
            raise ValueError("Lookup manifest exceeds the size limit")
        manifest = json.loads(manifest_path.read_text())
        expected, declared_hashes, configured = _expected_manifest(manifest)
        if not 100 <= batch_size <= 20_000:
            raise ValueError("Import batch size must be between 100 and 20000")
        archive_hash = hashlib.sha256()
        with archive_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                archive_hash.update(chunk)
        archive_digest = archive_hash.hexdigest()
        declared_archive_hash = manifest.get("archive_sha256", manifest.get("output_sha256"))
        if declared_archive_hash and declared_archive_hash != archive_digest:
            raise ValueError("Lookup archive SHA-256 does not match the manifest")

        counts = {kind: 0 for kind in KINDS}
        seen_hashes = set()
        ndjson_hash = hashlib.sha256()
        role_cache = {}
        with self.store.connection() as connection:
            postgres = hasattr(connection, "connection") and hasattr(connection.connection, "cursor")
            if postgres:
                connection.execute(
                    """CREATE TEMP TABLE lookup_import_stage(
                        id TEXT NOT NULL,kind TEXT NOT NULL,source_hash TEXT NOT NULL,
                        sheet TEXT NOT NULL,row_number INTEGER NOT NULL,payload TEXT NOT NULL,
                        terms TEXT NOT NULL) ON COMMIT DROP"""
                )
                raw_cursor = connection.connection.cursor()
                copy_context = raw_cursor.copy(
                    "COPY lookup_import_stage(id,kind,source_hash,sheet,row_number,payload,terms) FROM STDIN"
                )
            else:
                connection.execute(
                    """CREATE TEMP TABLE lookup_rows_stage(
                        id TEXT NOT NULL,kind TEXT NOT NULL,source_hash TEXT NOT NULL,
                        sheet TEXT NOT NULL,row_number INTEGER NOT NULL,payload TEXT NOT NULL)"""
                )
                connection.execute(
                    """CREATE TEMP TABLE lookup_terms_stage(
                        kind TEXT NOT NULL,term TEXT NOT NULL,row_id TEXT NOT NULL)"""
                )
                row_batch, term_batch = [], []
                copy_context = None
            try:
                copy = copy_context.__enter__() if copy_context else None
                with gzip.open(archive_path, "rb") as source:
                    for line_number, line in enumerate(source, 1):
                        if len(line) > MAX_LINE_BYTES:
                            raise ValueError(f"Lookup row {line_number} exceeds the size limit")
                        ndjson_hash.update(line)
                        try:
                            raw = json.loads(line)
                        except Exception:
                            raise ValueError(f"Lookup row {line_number} is not valid JSON") from None
                        prepared = _record(raw, configured, role_cache)
                        identifier, kind, source_hash, sheet, row_number, payload, terms = prepared
                        counts[kind] += 1;seen_hashes.add(source_hash)
                        if counts["item"] + counts["po"] > expected["total"]:
                            raise ValueError("Lookup archive contains more rows than the manifest")
                        if postgres:
                            copy.write_row((identifier, kind, source_hash, sheet, row_number,
                                            payload, json.dumps(terms, separators=(",", ":"))))
                        else:
                            row_batch.append((identifier, kind, source_hash, sheet, row_number, payload))
                            term_batch.extend((kind, term, identifier) for term in terms)
                            if len(row_batch) >= batch_size:
                                connection.executemany(
                                    "INSERT INTO lookup_rows_stage VALUES (?,?,?,?,?,?)", row_batch
                                )
                                connection.executemany(
                                    "INSERT INTO lookup_terms_stage VALUES (?,?,?)", term_batch
                                )
                                row_batch.clear();term_batch.clear()
                if copy_context:
                    copy_context.__exit__(None, None, None);copy_context = None
                elif row_batch:
                    connection.executemany("INSERT INTO lookup_rows_stage VALUES (?,?,?,?,?,?)", row_batch)
                    connection.executemany("INSERT INTO lookup_terms_stage VALUES (?,?,?)", term_batch)
                counts["total"] = counts["item"] + counts["po"]
                if counts != expected:
                    raise ValueError(f"Lookup row counts do not match manifest: expected {expected}, received {counts}")
                if seen_hashes != declared_hashes:
                    raise ValueError("Lookup source hashes do not match the manifest")
                ndjson_digest = ndjson_hash.hexdigest()
                if manifest.get("ndjson_sha256") and manifest["ndjson_sha256"] != ndjson_digest:
                    raise ValueError("Lookup NDJSON SHA-256 does not match the manifest")
                stage_table = "lookup_import_stage" if postgres else "lookup_rows_stage"
                duplicate = connection.execute(
                    f"""SELECT source_hash,sheet,row_number FROM {stage_table}
                        GROUP BY source_hash,sheet,row_number HAVING COUNT(*)>1 LIMIT 1"""
                ).fetchone()
                if duplicate:
                    raise ValueError("Lookup archive repeats a source hash, sheet and row")
                if postgres:
                    connection.execute(
                        """CREATE TEMP TABLE lookup_terms_stage(
                            kind TEXT NOT NULL,term TEXT NOT NULL,row_id TEXT NOT NULL)
                           ON COMMIT DROP"""
                    )
                    connection.execute(
                        """INSERT INTO lookup_terms_stage(kind,term,row_id)
                           SELECT s.kind,j.term,s.id FROM lookup_import_stage s
                           CROSS JOIN LATERAL jsonb_array_elements_text(s.terms::jsonb) AS j(term)"""
                    )
                connection.execute("DELETE FROM lookup_terms")
                connection.execute("DELETE FROM lookup_rows")
                connection.execute("DELETE FROM lookup_sources")
                if postgres:
                    connection.execute(
                        """INSERT INTO lookup_rows(id,kind,source_hash,sheet,row_number,payload)
                           SELECT id,kind,source_hash,sheet,row_number,payload FROM lookup_import_stage"""
                    )
                else:
                    connection.execute(
                        """INSERT INTO lookup_rows(id,kind,source_hash,sheet,row_number,payload)
                           SELECT id,kind,source_hash,sheet,row_number,payload FROM lookup_rows_stage"""
                    )
                if postgres:
                    # Terms are de-duplicated within every validated record and
                    # source provenance is unique, so an indexed staging table
                    # only adds random writes. Sort once while loading the real
                    # primary key to make the bulk B-tree build sequential.
                    connection.execute(
                        """INSERT INTO lookup_terms(kind,term,row_id)
                           SELECT kind,term,row_id FROM lookup_terms_stage
                           ORDER BY kind,term,row_id"""
                    )
                else:
                    connection.execute(
                        """INSERT INTO lookup_terms(kind,term,row_id)
                           SELECT kind,term,row_id FROM lookup_terms_stage"""
                    )
                imported_at = datetime.now(timezone.utc).isoformat()
                source = {"id": "dataset:" + ndjson_digest,
                          "version": manifest.get("version", manifest.get("manifest_version")),
                          "imported_at": imported_at, "counts": counts,
                          "source_hashes": sorted(seen_hashes), "archive_sha256": archive_digest,
                          "ndjson_sha256": ndjson_digest}
                connection.execute(
                    "INSERT INTO lookup_sources(id,payload) VALUES (?,?)",
                    (source["id"], json.dumps(source, separators=(",", ":"))),
                )
            except BaseException as error:
                if copy_context:
                    copy_context.__exit__(type(error), error, error.__traceback__)
                raise
        # Bulk replacement invalidates planner estimates. Run this only after
        # the import transaction commits so readers keep seeing one complete
        # catalog while the database refreshes its lookup-table statistics.
        with self.store.connection() as connection:
            connection.execute("ANALYZE lookup_rows")
            connection.execute("ANALYZE lookup_terms")
        return {"imported": True, "approved_for_matching": False,
                "requires_confirmation": True, "source": source}
