"""Resumable, lock-bounded PostgreSQL import for reference lookup catalogs.

The importer validates an archive without retaining its row identities in
memory, builds versioned indexed shadow tables in bounded committed batches,
and keeps the live catalog readable until a two-second atomic cutover.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.reference_lookup import KINDS, MAX_LINE_BYTES, _expected_manifest, _record


IMPORT_FORMAT_VERSION = "lookup-shadow.v2"
IMPORT_ADVISORY_LOCK = 4_285_711_449_731_903_217
MAX_BATCH_BYTES = 16 * 1024 * 1024
MAX_BATCH_TERMS = 50_000
VERIFY_TIMEOUT = "120s"
ANALYZE_TIMEOUT = "60s"
BATCH_SORT_WORK_MEM = "16MB"
PATTERN_INDEX_TIMEOUT = "5min"


class ShadowLookupImporter:
    """Populate and atomically publish a PostgreSQL lookup catalog."""

    def __init__(self, store):
        self.store = store

    def import_archive(
        self,
        archive_path: str | Path,
        manifest_path: str | Path,
        batch_size: int = 1_000,
        throttle_seconds: float = 0.02,
    ) -> dict[str, Any]:
        archive_path = Path(archive_path)
        manifest_path = Path(manifest_path)
        if manifest_path.stat().st_size > 1_000_000:
            raise ValueError("Lookup manifest exceeds the size limit")
        if not 100 <= batch_size <= 1_000:
            raise ValueError("PostgreSQL shadow import batch size must be between 100 and 1000")
        if not 0 <= throttle_seconds <= 5:
            raise ValueError("Lookup import throttle must be between 0 and 5 seconds")

        manifest = json.loads(manifest_path.read_text())
        expected, declared_hashes, configured = _expected_manifest(manifest)
        source_manifest, declared_source_counts = _manifest_sources(
            manifest, declared_hashes, expected
        )
        archive_digest = _file_sha256(archive_path)
        declared_archive_hash = manifest.get("archive_sha256", manifest.get("output_sha256"))
        if declared_archive_hash and declared_archive_hash != archive_digest:
            raise ValueError("Lookup archive SHA-256 does not match the manifest")
        identity = _import_identity(archive_digest, manifest)
        names = _shadow_names(identity)

        with self.store.connection() as connection:
            if not (
                hasattr(connection, "connection")
                and hasattr(connection.connection, "cursor")
            ):
                raise RuntimeError("Shadow lookup import requires PostgreSQL")
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(?)", (IMPORT_ADVISORY_LOCK,)
            ).fetchone()[0]
            if not acquired:
                raise RuntimeError("Another reference lookup import is already running")
            connection.connection.commit()
            try:
                validated = _validate_records(
                    archive_path,
                    manifest,
                    expected,
                    declared_hashes,
                    declared_source_counts,
                    configured,
                )
                return self._populate_and_cutover(
                    connection,
                    archive_path,
                    manifest,
                    configured,
                    expected,
                    declared_hashes,
                    declared_source_counts,
                    source_manifest,
                    archive_digest,
                    identity,
                    validated,
                    names,
                    batch_size,
                    throttle_seconds,
                )
            finally:
                # A statement error may have left the transaction aborted. The
                # session lock itself survives rollback and is then released.
                connection.connection.rollback()
                connection.execute(
                    "SELECT pg_advisory_unlock(?)", (IMPORT_ADVISORY_LOCK,)
                )
                connection.connection.commit()

    def _populate_and_cutover(
        self,
        connection,
        archive_path,
        manifest,
        configured,
        expected,
        declared_hashes,
        declared_source_counts,
        source_manifest,
        archive_digest,
        identity,
        validated,
        names,
        batch_size,
        throttle_seconds,
    ):
        del declared_source_counts
        counts, ndjson_digest, expected_terms, actual_source_counts = validated
        raw_connection = connection.connection
        _prepare_shadow(connection, names, archive_digest, identity)
        meta = _read_meta(connection, names)
        if (
            meta is None
            or meta["archive_sha256"] != archive_digest
            or meta["import_identity"] != identity
            or meta["format_version"] != IMPORT_FORMAT_VERSION
        ):
            _discard_shadow(connection, names)
            raise ValueError("Lookup shadow identity does not match this import")

        next_line = int(meta["next_line"])
        resumed_from = next_line
        if not 0 <= next_line <= expected["total"]:
            _discard_shadow(connection, names)
            raise ValueError("Lookup shadow progress exceeds the validated archive")

        if next_line:
            prefix = _prefix_state(archive_path, configured, next_line)
            _verify_resume_state(connection, names, meta, prefix)
            imported_counts = dict(prefix[0])
            imported_terms = prefix[1]
            imported_source_counts = dict(prefix[2])
        else:
            imported_counts = {kind: 0 for kind in KINDS}
            imported_terms = 0
            imported_source_counts = {}

        if not meta["complete"]:
            role_cache: dict[Any, Any] = {}
            batch: list[tuple] = []
            batch_bytes = 0
            batch_terms = 0
            batch_last_line = next_line
            with gzip.open(archive_path, "rb") as stream:
                for line_number, line in enumerate(stream, 1):
                    if line_number <= next_line:
                        continue
                    prepared = _record(json.loads(line), configured, role_cache)
                    record_bytes, record_terms = _record_load(prepared)
                    if record_terms > MAX_BATCH_TERMS or record_bytes > MAX_BATCH_BYTES:
                        raise ValueError("A lookup row exceeds the bounded batch limits")
                    if batch and (
                        len(batch) >= batch_size
                        or batch_bytes + record_bytes > MAX_BATCH_BYTES
                        or batch_terms + record_terms > MAX_BATCH_TERMS
                    ):
                        imported_terms = _advance_counts(
                            batch,
                            imported_counts,
                            imported_source_counts,
                            imported_terms,
                        )
                        _write_batch(
                            connection,
                            names,
                            batch,
                            batch_last_line,
                            imported_counts,
                            imported_terms,
                        )
                        batch.clear()
                        batch_bytes = 0
                        batch_terms = 0
                        time.sleep(throttle_seconds)
                    batch.append(prepared)
                    batch_bytes += record_bytes
                    batch_terms += record_terms
                    batch_last_line = line_number
                if batch:
                    imported_terms = _advance_counts(
                        batch,
                        imported_counts,
                        imported_source_counts,
                        imported_terms,
                    )
                    _write_batch(
                        connection,
                        names,
                        batch,
                        batch_last_line,
                        imported_counts,
                        imported_terms,
                    )

            expected_kind_counts = {kind: expected[kind] for kind in KINDS}
            if imported_counts != expected_kind_counts:
                _discard_shadow(connection, names)
                raise ValueError("Lookup shadow row counts do not match the validated archive")
            if imported_terms != expected_terms:
                _discard_shadow(connection, names)
                raise ValueError("Lookup shadow term count does not match the validated archive")
            if imported_source_counts != actual_source_counts:
                _discard_shadow(connection, names)
                raise ValueError("Lookup shadow source counts do not match the validated archive")
            _verify_completed_rows(
                connection, names, counts, actual_source_counts, declared_hashes
            )
            source = {
                "id": "dataset:" + ndjson_digest,
                "version": manifest.get("version", manifest.get("manifest_version")),
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "counts": counts,
                "source_counts": actual_source_counts,
                "manifest_sources": source_manifest,
                "archive_sha256": archive_digest,
                "ndjson_sha256": ndjson_digest,
                "import_identity": identity,
                "import_format": IMPORT_FORMAT_VERSION,
            }
            connection.execute(f"DELETE FROM {names['sources']}")
            connection.execute(
                f"INSERT INTO {names['sources']}(id,payload) VALUES (?,?)",
                (source["id"], json.dumps(source, separators=(",", ":"))),
            )
            connection.execute(
                f"UPDATE {names['meta']} SET complete=? WHERE singleton=1", (True,)
            )
            raw_connection.commit()
        else:
            source_row = connection.execute(
                f"SELECT payload FROM {names['sources']} ORDER BY id LIMIT 1"
            ).fetchone()
            if source_row is None:
                _discard_shadow(connection, names)
                raise ValueError("Completed lookup shadow has no provenance record")
            source = json.loads(source_row["payload"])
            if (
                source.get("archive_sha256") != archive_digest
                or source.get("import_identity") != identity
                or source.get("import_format") != IMPORT_FORMAT_VERSION
            ):
                _discard_shadow(connection, names)
                raise ValueError("Completed lookup shadow provenance is invalid")

        # Bounded planner refreshes operate only on shadow relations. A timeout
        # retains the complete shadow so an operator can retry cutover later.
        _analyze_shadow(connection, names["rows"])
        _analyze_shadow(connection, names["terms"])
        _cutover(connection, names)
        return {
            "imported": True,
            "approved_for_matching": False,
            "requires_confirmation": True,
            "resumed_from_line": resumed_from,
            "batch_size": batch_size,
            "source": source,
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _import_identity(archive_digest, manifest):
    canonical = json.dumps(
        {
            "archive_sha256": archive_digest,
            "format_version": IMPORT_FORMAT_VERSION,
            "manifest": manifest,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _manifest_sources(manifest, declared_hashes, expected):
    entries = manifest.get("sources")
    if not isinstance(entries, list) or not entries:
        return [], None
    result = []
    counts: dict[str, int] = {}
    has_count = []
    seen_hashes = set()
    seen_entries = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Manifest sources must contain objects")
        source_hash = entry.get("source_hash", entry.get("hash"))
        if source_hash not in declared_hashes:
            raise ValueError("Manifest source entry has an undeclared hash")
        seen_hashes.add(source_hash)
        clean = {"hash": source_hash}
        for field in ("label", "sheet"):
            if field in entry:
                value = entry[field]
                if (
                    not isinstance(value, str)
                    or not 1 <= len(value) <= 500
                    or any(ord(character) < 0x20 for character in value)
                ):
                    raise ValueError(f"Manifest source {field} is invalid")
                clean[field] = value
        present = "count" in entry
        has_count.append(present)
        if present:
            count = entry["count"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("Manifest source count must be a non-negative integer")
            clean["count"] = count
            counts[source_hash] = counts.get(source_hash, 0) + count
        entry_key = (source_hash, clean.get("label"), clean.get("sheet"))
        if entry_key in seen_entries:
            raise ValueError("Manifest sources repeat the same source descriptor")
        seen_entries.add(entry_key)
        result.append(clean)
    if seen_hashes != declared_hashes:
        raise ValueError("Manifest source entries do not cover every declared hash")
    if any(has_count) and not all(has_count):
        raise ValueError("Manifest source counts must be declared for every source")
    if all(has_count) and sum(counts.values()) != expected["total"]:
        raise ValueError("Manifest source counts do not match the expected total")
    return result, counts if all(has_count) else None


def _validate_records(
    archive_path,
    manifest,
    expected,
    declared_hashes,
    declared_source_counts,
    configured,
):
    counts = {kind: 0 for kind in KINDS}
    source_counts: dict[str, int] = {}
    seen_hashes = set()
    ndjson_hash = hashlib.sha256()
    term_count = 0
    role_cache: dict[Any, Any] = {}
    with tempfile.TemporaryDirectory(prefix="lookup-validate-") as directory:
        database = sqlite3.connect(Path(directory) / "identities.sqlite3")
        database.execute("PRAGMA journal_mode=OFF")
        database.execute("PRAGMA synchronous=OFF")
        database.execute(
            """CREATE TABLE seen(
                source_hash TEXT NOT NULL,sheet TEXT NOT NULL,row_number INTEGER NOT NULL,
                PRIMARY KEY(source_hash,sheet,row_number)) WITHOUT ROWID"""
        )
        try:
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
                    _, kind, source_hash, sheet, row_number, _, terms = prepared
                    try:
                        database.execute(
                            "INSERT INTO seen VALUES (?,?,?)",
                            (source_hash, sheet, row_number),
                        )
                    except sqlite3.IntegrityError:
                        raise ValueError(
                            "Lookup archive repeats a source hash, sheet and row"
                        ) from None
                    counts[kind] += 1
                    source_counts[source_hash] = source_counts.get(source_hash, 0) + 1
                    seen_hashes.add(source_hash)
                    term_count += len(terms)
                    if counts["item"] + counts["po"] > expected["total"]:
                        raise ValueError("Lookup archive contains more rows than the manifest")
                    if line_number % 5_000 == 0:
                        database.commit()
            database.commit()
        finally:
            database.close()
    counts["total"] = counts["item"] + counts["po"]
    if counts != expected:
        raise ValueError(
            f"Lookup row counts do not match manifest: expected {expected}, received {counts}"
        )
    if seen_hashes != declared_hashes:
        raise ValueError("Lookup source hashes do not match the manifest")
    if declared_source_counts is not None and source_counts != declared_source_counts:
        raise ValueError("Lookup source counts do not match the manifest")
    ndjson_digest = ndjson_hash.hexdigest()
    if manifest.get("ndjson_sha256") and manifest["ndjson_sha256"] != ndjson_digest:
        raise ValueError("Lookup NDJSON SHA-256 does not match the manifest")
    return counts, ndjson_digest, term_count, source_counts


def _prefix_state(archive_path, configured, next_line):
    counts = {kind: 0 for kind in KINDS}
    source_counts: dict[str, int] = {}
    term_count = 0
    role_cache: dict[Any, Any] = {}
    with gzip.open(archive_path, "rb") as source:
        for line_number, line in enumerate(source, 1):
            if line_number > next_line:
                break
            prepared = _record(json.loads(line), configured, role_cache)
            _, kind, source_hash, _, _, _, terms = prepared
            counts[kind] += 1
            source_counts[source_hash] = source_counts.get(source_hash, 0) + 1
            term_count += len(terms)
    if sum(counts.values()) != next_line:
        raise ValueError("Lookup shadow progress is beyond the archive")
    return counts, term_count, source_counts


def _shadow_names(identity):
    suffix = identity[:16]
    rows = f"lookup_rows_shadow_{suffix}"
    terms = f"lookup_terms_shadow_{suffix}"
    sources = f"lookup_sources_shadow_{suffix}"
    return {
        "rows": rows,
        "terms": terms,
        "sources": sources,
        "meta": f"lookup_import_meta_{suffix}",
        "batch_rows": f"lookup_rows_batch_{suffix}",
        "batch_terms": f"lookup_terms_batch_{suffix}",
        "rows_kind": f"{rows}_kind_id",
        "rows_source": f"{rows}_source_row",
        "terms_pattern": f"{terms}_kind_term_pattern",
    }


def _prepare_shadow(connection, names, archive_digest, identity):
    table_keys = ("sources", "rows", "terms", "meta", "batch_rows", "batch_terms")
    table_names = [names[key] for key in table_keys]
    placeholders = ",".join("?" for _ in table_names)
    existing = {
        row["relname"]
        for row in connection.execute(
            f"""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=current_schema() AND c.relname IN ({placeholders})""",
            tuple(table_names),
        )
    }
    if existing and existing != set(table_names):
        _discard_shadow(connection, names)
        raise ValueError("Lookup shadow schema is incomplete and was discarded")
    if not existing:
        connection.execute(
            f"CREATE TABLE {names['sources']}(id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
        )
        connection.execute(
            f"""CREATE TABLE {names['rows']}(
                id TEXT PRIMARY KEY,kind TEXT NOT NULL,source_hash TEXT NOT NULL,
                sheet TEXT NOT NULL,row_number INTEGER NOT NULL,payload TEXT NOT NULL)"""
        )
        connection.execute(
            f"""CREATE TABLE {names['terms']}(
                kind TEXT NOT NULL,term TEXT NOT NULL,row_id TEXT NOT NULL,
                PRIMARY KEY(kind,term,row_id))"""
        )
        connection.execute(
            f"CREATE INDEX {names['rows_kind']} ON {names['rows']}(kind,id)"
        )
        connection.execute(
            f"""CREATE UNIQUE INDEX {names['rows_source']}
                ON {names['rows']}(source_hash,sheet,row_number)"""
        )
        connection.execute(
            f"""CREATE TABLE {names['meta']}(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                archive_sha256 TEXT NOT NULL,import_identity TEXT NOT NULL,
                format_version TEXT NOT NULL,next_line INTEGER NOT NULL,
                item_count INTEGER NOT NULL,po_count INTEGER NOT NULL,
                term_count BIGINT NOT NULL,complete BOOLEAN NOT NULL)"""
        )
        connection.execute(
            f"""CREATE UNLOGGED TABLE {names['batch_rows']}(
                id TEXT NOT NULL,kind TEXT NOT NULL,source_hash TEXT NOT NULL,
                sheet TEXT NOT NULL,row_number INTEGER NOT NULL,payload TEXT NOT NULL)"""
        )
        connection.execute(
            f"""CREATE UNLOGGED TABLE {names['batch_terms']}(
                kind TEXT NOT NULL,term TEXT NOT NULL,row_id TEXT NOT NULL)"""
        )
        connection.execute(
            f"""INSERT INTO {names['meta']}
                (singleton,archive_sha256,import_identity,format_version,next_line,
                 item_count,po_count,term_count,complete)
                VALUES (1,?,?,?,0,0,0,0,?)""",
            (archive_digest, identity, IMPORT_FORMAT_VERSION, False),
        )
        connection.connection.commit()
    # Upgrade an existing validated/resumable shadow in place. This index is
    # built only on the versioned shadow relation, never on the live lookup
    # table. New shadows create it while empty; old partial shadows may need a
    # one-time bounded build before batch loading resumes.
    connection.execute(f"SET LOCAL statement_timeout='{PATTERN_INDEX_TIMEOUT}'")
    connection.execute(
        f"""CREATE INDEX IF NOT EXISTS {names['terms_pattern']}
            ON {names['terms']}(kind,term text_pattern_ops,row_id)"""
    )
    connection.connection.commit()
    try:
        _verify_shadow_schema(connection, names)
    except ValueError:
        _discard_shadow(connection, names)
        raise


def _verify_shadow_schema(connection, names):
    expected_columns = {
        names["sources"]: [("id", "text", True), ("payload", "text", True)],
        names["rows"]: [
            ("id", "text", True),
            ("kind", "text", True),
            ("source_hash", "text", True),
            ("sheet", "text", True),
            ("row_number", "integer", True),
            ("payload", "text", True),
        ],
        names["terms"]: [
            ("kind", "text", True),
            ("term", "text", True),
            ("row_id", "text", True),
        ],
        names["meta"]: [
            ("singleton", "integer", True),
            ("archive_sha256", "text", True),
            ("import_identity", "text", True),
            ("format_version", "text", True),
            ("next_line", "integer", True),
            ("item_count", "integer", True),
            ("po_count", "integer", True),
            ("term_count", "bigint", True),
            ("complete", "boolean", True),
        ],
        names["batch_rows"]: [
            ("id", "text", True),
            ("kind", "text", True),
            ("source_hash", "text", True),
            ("sheet", "text", True),
            ("row_number", "integer", True),
            ("payload", "text", True),
        ],
        names["batch_terms"]: [
            ("kind", "text", True),
            ("term", "text", True),
            ("row_id", "text", True),
        ],
    }
    rows = connection.execute(
        """SELECT c.relname,c.relpersistence,a.attname,
                  format_type(a.atttypid,a.atttypmod) AS data_type,a.attnotnull
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           JOIN pg_attribute a ON a.attrelid=c.oid
           WHERE n.nspname=current_schema() AND c.relname LIKE 'lookup_%%'
             AND a.attnum>0 AND NOT a.attisdropped
           ORDER BY c.relname,a.attnum"""
    ).fetchall()
    actual_columns: dict[str, list[tuple]] = {}
    persistence = {}
    for row in rows:
        if row["relname"] in expected_columns:
            persistence[row["relname"]] = row["relpersistence"]
            actual_columns.setdefault(row["relname"], []).append(
                (row["attname"], row["data_type"], bool(row["attnotnull"]))
            )
    expected_persistence = {
        name: ("u" if name in (names["batch_rows"], names["batch_terms"]) else "p")
        for name in expected_columns
    }
    if actual_columns != expected_columns or persistence != expected_persistence:
        raise ValueError("Lookup shadow table definitions are incompatible")

    index_rows = connection.execute(
        """SELECT ci.relname AS index_name,ct.relname AS table_name,
                  i.indisunique,i.indisprimary,i.indisvalid,
                  array_agg(a.attname ORDER BY keys.ordinality) AS columns,
                  array_agg(opc.opcname ORDER BY keys.ordinality) AS opclasses
           FROM pg_index i JOIN pg_class ci ON ci.oid=i.indexrelid
           JOIN pg_class ct ON ct.oid=i.indrelid
           JOIN pg_namespace n ON n.oid=ct.relnamespace
           CROSS JOIN LATERAL unnest(i.indkey::smallint[],i.indclass::oid[])
                WITH ORDINALITY AS keys(attnum,opclass_oid,ordinality)
           JOIN pg_attribute a ON a.attrelid=ct.oid AND a.attnum=keys.attnum
           JOIN pg_opclass opc ON opc.oid=keys.opclass_oid
           WHERE n.nspname=current_schema() AND ct.relname LIKE 'lookup_%%'
           GROUP BY ci.relname,ct.relname,i.indisunique,i.indisprimary,i.indisvalid"""
    ).fetchall()
    actual_indexes = {
        row["index_name"]: (
            row["table_name"],
            bool(row["indisunique"]),
            bool(row["indisprimary"]),
            bool(row["indisvalid"]),
            tuple(row["columns"]),
            tuple(row["opclasses"]),
        )
        for row in index_rows
        if row["table_name"] in expected_columns
    }
    expected_indexes = {
        names["sources"] + "_pkey": (
            names["sources"], True, True, True, ("id",), ("text_ops",)
        ),
        names["rows"] + "_pkey": (
            names["rows"], True, True, True, ("id",), ("text_ops",)
        ),
        names["terms"] + "_pkey": (
            names["terms"],
            True,
            True,
            True,
            ("kind", "term", "row_id"),
            ("text_ops", "text_ops", "text_ops"),
        ),
        names["terms_pattern"]: (
            names["terms"],
            False,
            False,
            True,
            ("kind", "term", "row_id"),
            ("text_ops", "text_pattern_ops", "text_ops"),
        ),
        names["meta"] + "_pkey": (
            names["meta"], True, True, True, ("singleton",), ("int4_ops",)
        ),
        names["rows_kind"]: (
            names["rows"], False, False, True, ("kind", "id"),
            ("text_ops", "text_ops"),
        ),
        names["rows_source"]: (
            names["rows"],
            True,
            False,
            True,
            ("source_hash", "sheet", "row_number"),
            ("text_ops", "text_ops", "int4_ops"),
        ),
    }
    if actual_indexes != expected_indexes:
        raise ValueError("Lookup shadow index definitions are incompatible")


def _read_meta(connection, names):
    return connection.execute(
        f"""SELECT archive_sha256,import_identity,format_version,next_line,
                   item_count,po_count,term_count,complete
            FROM {names['meta']} WHERE singleton=1"""
    ).fetchone()


def _record_load(record):
    payload = record[5]
    terms = record[6]
    return len(payload.encode()) + sum(len(term.encode()) + 24 for term in terms), len(terms)


def _advance_counts(batch, counts, source_counts, term_count):
    for record in batch:
        counts[record[1]] += 1
        source_counts[record[2]] = source_counts.get(record[2], 0) + 1
        term_count += len(record[6])
    return term_count


def _write_batch(connection, names, batch, line_number, counts, term_count):
    raw_connection = connection.connection
    expected_terms = sum(len(record[6]) for record in batch)
    try:
        connection.execute(f"TRUNCATE {names['batch_rows']},{names['batch_terms']}")
        with raw_connection.cursor().copy(
            f"""COPY {names['batch_rows']}
                (id,kind,source_hash,sheet,row_number,payload) FROM STDIN"""
        ) as copy:
            for identifier, kind, source_hash, sheet, row_number, payload, _ in batch:
                copy.write_row((identifier, kind, source_hash, sheet, row_number, payload))
        with raw_connection.cursor().copy(
            f"COPY {names['batch_terms']}(kind,term,row_id) FROM STDIN"
        ) as copy:
            for identifier, kind, _, _, _, _, terms in batch:
                for term in terms:
                    copy.write_row((kind, term, identifier))
        row_cursor = connection.execute(
            f"""INSERT INTO {names['rows']}(id,kind,source_hash,sheet,row_number,payload)
                SELECT id,kind,source_hash,sheet,row_number,payload
                FROM {names['batch_rows']}"""
        )
        # The term table's primary key has term before row_id. Archive order is
        # source-row order, so inserting the staging heap directly causes
        # scattered B-tree writes once the shadow index is large. Sort only the
        # current bounded batch (at most MAX_BATCH_TERMS / MAX_BATCH_BYTES),
        # never the full catalog. One import session gets a conservative local
        # sort budget which is released at this batch commit.
        connection.execute(f"SET LOCAL work_mem='{BATCH_SORT_WORK_MEM}'")
        term_cursor = connection.execute(
            f"""INSERT INTO {names['terms']}(kind,term,row_id)
                SELECT kind,term,row_id FROM {names['batch_terms']}
                ORDER BY kind,term,row_id"""
        )
        if row_cursor.cursor.rowcount != len(batch) or term_cursor.cursor.rowcount != expected_terms:
            raise RuntimeError("Lookup shadow batch insert count is inconsistent")
        connection.execute(
            f"""UPDATE {names['meta']} SET next_line=?,item_count=?,po_count=?,term_count=?
                WHERE singleton=1""",
            (line_number, counts["item"], counts["po"], term_count),
        )
        raw_connection.commit()
    except BaseException:
        raw_connection.rollback()
        raise


def _actual_shadow_state(connection, names):
    raw_connection = connection.connection
    try:
        connection.execute(f"SET LOCAL statement_timeout='{VERIFY_TIMEOUT}'")
        counts = {kind: 0 for kind in KINDS}
        for row in connection.execute(
            f"SELECT kind,COUNT(*) AS count FROM {names['rows']} GROUP BY kind"
        ):
            counts[row["kind"]] = int(row["count"])
        terms = int(
            connection.execute(
                f"SELECT COUNT(*) AS count FROM {names['terms']}"
            ).fetchone()["count"]
        )
        source_counts = {
            row["source_hash"]: int(row["count"])
            for row in connection.execute(
                f"""SELECT source_hash,COUNT(*) AS count FROM {names['rows']}
                    GROUP BY source_hash"""
            )
        }
        sources = int(
            connection.execute(
                f"SELECT COUNT(*) AS count FROM {names['sources']}"
            ).fetchone()["count"]
        )
        raw_connection.commit()
        return counts, terms, source_counts, sources
    except Exception as error:
        raw_connection.rollback()
        raise RuntimeError(
            "Lookup shadow verification timed out or failed; shadow catalog retained"
        ) from error


def _verify_resume_state(connection, names, meta, prefix):
    expected_counts, expected_terms, expected_sources = prefix
    meta_counts = {"item": int(meta["item_count"]), "po": int(meta["po_count"])}
    expected_source_rows = 1 if meta["complete"] else 0
    actual_counts, actual_terms, actual_sources, source_rows = _actual_shadow_state(
        connection, names
    )
    if (
        int(meta["next_line"]) != sum(expected_counts.values())
        or meta_counts != expected_counts
        or int(meta["term_count"]) != expected_terms
        or actual_counts != expected_counts
        or actual_terms != expected_terms
        or actual_sources != expected_sources
        or source_rows != expected_source_rows
    ):
        _discard_shadow(connection, names)
        raise ValueError("Lookup shadow progress is inconsistent and was discarded")


def _verify_completed_rows(connection, names, expected, source_counts, declared_hashes):
    raw_connection = connection.connection
    try:
        connection.execute(f"SET LOCAL statement_timeout='{VERIFY_TIMEOUT}'")
        counts = {kind: 0 for kind in KINDS}
        for row in connection.execute(
            f"SELECT kind,COUNT(*) AS count FROM {names['rows']} GROUP BY kind"
        ):
            counts[row["kind"]] = int(row["count"])
        counts["total"] = sum(counts.values())
        actual_sources = {
            row["source_hash"]: int(row["count"])
            for row in connection.execute(
                f"""SELECT source_hash,COUNT(*) AS count FROM {names['rows']}
                    GROUP BY source_hash"""
            )
        }
        raw_connection.commit()
    except Exception as error:
        raw_connection.rollback()
        raise RuntimeError(
            "Lookup shadow row verification timed out or failed; shadow catalog retained"
        ) from error
    if (
        counts != expected
        or actual_sources != source_counts
        or set(actual_sources) != declared_hashes
    ):
        _discard_shadow(connection, names)
        raise ValueError("Lookup shadow row verification failed")


def _analyze_shadow(connection, table):
    raw_connection = connection.connection
    try:
        connection.execute(f"SET LOCAL statement_timeout='{ANALYZE_TIMEOUT}'")
        connection.execute(f"ANALYZE {table}")
        raw_connection.commit()
    except Exception as error:
        raw_connection.rollback()
        raise RuntimeError(
            "Lookup shadow analysis timed out or failed; completed shadow retained"
        ) from error


def _discard_shadow(connection, names):
    connection.connection.rollback()
    connection.execute(
        "DROP TABLE IF EXISTS "
        + ",".join(
            names[key]
            for key in ("batch_terms", "batch_rows", "meta", "terms", "rows", "sources")
        )
    )
    connection.connection.commit()


def _quote_identifier(connection, value):
    return connection.execute("SELECT quote_ident(?)", (value,)).fetchone()[0]


def _table_security(connection):
    owners = {
        row["relname"]: row["owner"]
        for row in connection.execute(
            """SELECT c.relname,pg_get_userbyid(c.relowner) AS owner
               FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname=current_schema()
                 AND c.relname IN ('lookup_sources','lookup_rows','lookup_terms')"""
        )
    }
    grants = []
    for row in connection.execute(
        """SELECT c.relname,x.grantee,
                  CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END AS grantee_name,
                  x.privilege_type,x.is_grantable
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           CROSS JOIN LATERAL aclexplode(c.relacl) x
           WHERE n.nspname=current_schema()
             AND c.relname IN ('lookup_sources','lookup_rows','lookup_terms')"""
    ):
        grants.append(
            (
                row["relname"],
                row["grantee_name"],
                row["privilege_type"],
                bool(row["is_grantable"]),
            )
        )
    if set(owners) != {"lookup_sources", "lookup_rows", "lookup_terms"}:
        raise RuntimeError("Live lookup table ownership could not be captured")
    return owners, grants


def _apply_security(connection, names, security):
    owners, grants = security
    live_to_shadow = {
        "lookup_sources": names["sources"],
        "lookup_rows": names["rows"],
        "lookup_terms": names["terms"],
    }
    for live_name, shadow_name in live_to_shadow.items():
        quoted_table = _quote_identifier(connection, shadow_name)
        current_grantees = {
            row["grantee_name"]
            for row in connection.execute(
                """SELECT CASE WHEN x.grantee=0 THEN 'PUBLIC'
                               ELSE pg_get_userbyid(x.grantee) END AS grantee_name
                   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   CROSS JOIN LATERAL aclexplode(c.relacl) x
                   WHERE n.nspname=current_schema() AND c.relname=?""",
                (shadow_name,),
            )
        }
        current_grantees.add("PUBLIC")
        for grantee in current_grantees:
            quoted_grantee = "PUBLIC" if grantee == "PUBLIC" else _quote_identifier(
                connection, grantee
            )
            connection.execute(
                f"REVOKE ALL PRIVILEGES ON TABLE {quoted_table} FROM {quoted_grantee}"
            )
        quoted_owner = _quote_identifier(connection, owners[live_name])
        connection.execute(f"ALTER TABLE {quoted_table} OWNER TO {quoted_owner}")
        for table, grantee, privilege, grantable in grants:
            if table != live_name:
                continue
            quoted_grantee = "PUBLIC" if grantee == "PUBLIC" else _quote_identifier(
                connection, grantee
            )
            suffix = " WITH GRANT OPTION" if grantable else ""
            connection.execute(
                f"GRANT {privilege} ON TABLE {quoted_table} TO {quoted_grantee}{suffix}"
            )


def _cutover(connection, names):
    raw_connection = connection.connection
    try:
        connection.execute("SET LOCAL lock_timeout='2s'")
        connection.execute(
            "LOCK TABLE lookup_sources,lookup_rows,lookup_terms IN ACCESS EXCLUSIVE MODE"
        )
        security = _table_security(connection)
        _apply_security(connection, names, security)
        connection.execute("DROP TABLE lookup_terms,lookup_rows,lookup_sources")
        connection.execute(f"ALTER TABLE {names['sources']} RENAME TO lookup_sources")
        connection.execute(f"ALTER TABLE {names['rows']} RENAME TO lookup_rows")
        connection.execute(f"ALTER TABLE {names['terms']} RENAME TO lookup_terms")
        connection.execute(
            f"ALTER INDEX {names['sources']}_pkey RENAME TO lookup_sources_pkey"
        )
        connection.execute(f"ALTER INDEX {names['rows']}_pkey RENAME TO lookup_rows_pkey")
        connection.execute(f"ALTER INDEX {names['terms']}_pkey RENAME TO lookup_terms_pkey")
        connection.execute(
            f"ALTER INDEX {names['terms_pattern']} "
            "RENAME TO lookup_terms_kind_term_pattern"
        )
        connection.execute(
            f"ALTER INDEX {names['rows_kind']} RENAME TO lookup_rows_kind_id"
        )
        connection.execute(
            f"ALTER INDEX {names['rows_source']} RENAME TO lookup_rows_source_row"
        )
        connection.execute(
            "DROP TABLE "
            + ",".join(names[key] for key in ("batch_terms", "batch_rows", "meta"))
        )
        raw_connection.commit()
    except Exception as error:
        raw_connection.rollback()
        if getattr(error, "sqlstate", None) == "55P03":
            raise RuntimeError(
                "Lookup cutover could not acquire its two-second lock; shadow catalog retained"
            ) from None
        raise RuntimeError("Lookup cutover failed; shadow catalog retained") from error
