"""Permanent invoice deletion with minimal audit and receipt tombstones.

Live job payloads, source uploads, audit evidence, generated exports and complete
selected-batch workbooks are removed. For an approved export, only the invoice
key and PO-line quantity allocations needed for duplicate and receipt controls
remain. Provider backups, logs, object versions, and soft-delete retention are
outside this live-store operation and follow each provider's retention policy.
"""

from __future__ import annotations

import json
from pathlib import Path


ACTIVE_STATUSES = frozenset(("queued", "processing"))
BACKUP_NOTICE = (
    "Live application records and stored objects were removed. "
    "Provider backups, logs, object versions, or soft-deleted copies are not erased by "
    "this action and follow each provider's retention policy."
)


class DeletionError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **detail):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"code": code, "message": message, **detail}


def delete_invoices(store, selections, connection):
    """Delete already-confirmed selections inside the caller's write transaction."""

    selected = {_value(item, "id"): item for item in selections}
    jobs = {}
    missing = []
    for job_id, request in selected.items():
        job = store.job(job_id, c=connection)
        if job is None:
            missing.append(job_id)
            continue
        if job.get("status") in ACTIVE_STATUSES:
            raise DeletionError(
                409,
                "INVOICE_ACTIVE",
                "Wait for every selected invoice to finish processing before deleting it.",
                job_ids=[job_id],
            )
        if job.get("revision") != _value(request, "revision"):
            raise DeletionError(
                409,
                "REVISION_CHANGED",
                "A selected invoice changed. Refresh the list before deleting.",
                job_ids=[job_id],
            )
        jobs[job_id] = job
    if missing:
        raise DeletionError(
            404,
            "INVOICE_NOT_FOUND",
            "One or more selected invoices no longer exist.",
            job_ids=sorted(missing),
        )

    exports = _exports(connection)
    export_by_job = {row["job_id"]: row for row in exports}
    selected_ids = set(selected)
    relevant_batches = {
        str(value)
        for job_id, job in jobs.items()
        for value in (
            job.get("batch_id"),
            export_by_job.get(job_id, {}).get("receipt", {}).get("batch_id"),
        )
        if value
    }
    batch_members = _batch_members(connection, relevant_batches)
    for row in exports:
        batch_id = row["receipt"].get("batch_id")
        if batch_id:
            batch_members.setdefault(str(batch_id), set()).add(row["job_id"])
    selected_batches = set()
    for job_id, job in jobs.items():
        exported = export_by_job.get(job_id)
        job_batch = str(job["batch_id"]) if job.get("batch_id") else None
        receipt_batch = (
            str(exported["receipt"]["batch_id"])
            if exported and exported["receipt"].get("batch_id")
            else None
        )
        if job_batch != receipt_batch:
            raise DeletionError(
                409,
                "EXPORT_INCONSISTENT",
                "The exported invoice records are incomplete. Deletion was not started.",
                job_ids=[job_id],
            )
        batch_id = job_batch
        if not batch_id:
            continue
        batch_id = str(batch_id)
        required = batch_members.get(batch_id, set())
        if not required or job_id not in required:
            raise DeletionError(
                409,
                "EXPORT_INCONSISTENT",
                "The exported invoice records are incomplete. Deletion was not started.",
                job_ids=[job_id],
            )
        if not required.issubset(selected_ids):
            raise DeletionError(
                409,
                "PARTIAL_EXPORTED_BATCH",
                "Select every invoice in the exported batch to delete it.",
                batch_id=batch_id,
                required_job_ids=sorted(required),
            )
        selected_batches.add(batch_id)

    for job_id, job in jobs.items():
        export_id = job.get("export_id")
        if export_id and (
            job_id not in export_by_job or export_by_job[job_id]["id"] != export_id
        ):
            raise DeletionError(
                409,
                "EXPORT_INCONSISTENT",
                "The exported invoice records are incomplete. Deletion was not started.",
                job_ids=[job_id],
            )

    prepared_tombstones = {}
    for job_id in jobs:
        exported = export_by_job.get(job_id)
        invoice_key = None
        tombstone = {"job_id": job_id, "deleted": True}
        if exported is not None:
            invoice_key = exported["invoice_key"]
            tombstone.update(
                invoice_key=invoice_key,
                allocations=_minimal_allocations(exported["receipt"].get("allocations")),
            )
        prepared_tombstones[job_id] = (exported, invoice_key, tombstone)

    # Source objects are removed by remove_uploads only after this transaction commits:
    # removing them first lost every selected source file when a later statement failed.
    upload_paths = _upload_paths(store, jobs.values())

    ledger_tombstones = 0
    deleted_exports = 0
    for job_id, job in jobs.items():
        exported, invoice_key, tombstone = prepared_tombstones[job_id]
        if exported is not None:
            ledger_tombstones += 1
            deleted_exports += 1
            connection.execute("DELETE FROM exports WHERE id=?", (exported["id"],))
            invoice_key, tombstone = _merge_into_existing_tombstone(
                connection, job_id, invoice_key, tombstone
            )
        connection.execute(
            """INSERT INTO deleted_invoices(job_id,invoice_key,payload)
               VALUES (?,?,?)""",
            (job_id, invoice_key, json.dumps(tombstone, separators=(",", ":"))),
        )
        connection.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    for batch_id in selected_batches:
        connection.execute("DELETE FROM batches WHERE id=?", (batch_id,))

    _remove_invoice_audit(connection, selected_ids, jobs.values())
    for job_id, job in jobs.items():
        store.audit(
            "invoice_deleted",
            {
                "job_id": job_id,
                "revision": job["revision"],
                "ledger_receipt_retained": job_id in export_by_job,
            },
            connection,
        )

    deleted_ids = sorted(selected_ids)
    return {
        "deleted_ids": deleted_ids,
        "count": len(deleted_ids),
        "ledger_tombstones": ledger_tombstones,
        "deleted_exports": deleted_exports,
        "deleted_batches": len(selected_batches),
        "retained": {
            "deletion_audit": len(deleted_ids),
            "ledger_receipts": ledger_tombstones,
        },
        "backup_notice": BACKUP_NOTICE,
        "upload_paths": upload_paths,
    }


def remove_uploads(store, paths):
    """Remove source objects after the deletion committed. A missing object counts as removed
    (a retry after a partial delete); any other failure is counted and audited, never raised."""

    failed = 0
    for path in paths:
        try:
            store.delete_upload(path)
        except Exception as error:
            failed += 1
            store.audit("invoice_upload_delete_failed", {"error_type": type(error).__name__})
    return failed


def _merge_into_existing_tombstone(connection, job_id, invoice_key, tombstone):
    """deleted_invoices.invoice_key is UNIQUE: when this invoice key was deleted before, its
    tombstone keeps the key and gains these allocations, so the receipt control still counts
    both; this job's own record stays, without the key."""

    existing = connection.execute(
        "SELECT job_id,payload FROM deleted_invoices WHERE invoice_key=?", (invoice_key,)
    ).fetchone()
    if existing is None:
        return invoice_key, tombstone
    kept = json.loads(existing["payload"])
    kept["allocations"] = list(kept.get("allocations") or []) + tombstone["allocations"]
    connection.execute(
        "UPDATE deleted_invoices SET payload=? WHERE job_id=?",
        (json.dumps(kept, separators=(",", ":")), existing["job_id"]),
    )
    return None, {"job_id": job_id, "deleted": True, "invoice_key_kept_by": existing["job_id"]}


def _value(item, key):
    return item[key] if isinstance(item, dict) else getattr(item, key)


def _exports(connection):
    result = []
    for row in connection.execute(
        "SELECT id,invoice_key,job_id,payload FROM exports"
    ).fetchall():
        result.append(
            {
                "id": row["id"],
                "invoice_key": row["invoice_key"],
                "job_id": row["job_id"],
                "receipt": json.loads(row["payload"]),
            }
        )
    return result


def _batch_members(connection, relevant_batches):
    result: dict[str, set[str]] = {}
    for row in connection.execute("SELECT id,payload FROM batches").fetchall():
        if str(row["id"]) not in relevant_batches:
            continue
        try:
            receipts = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            receipts = None
        if not isinstance(receipts, list):
            raise DeletionError(
                409,
                "EXPORT_INCONSISTENT",
                "An exported batch record is incomplete. Deletion was not started.",
                batch_id=row["id"],
            )
        members = {
            receipt.get("job_id")
            for receipt in receipts
            if isinstance(receipt, dict) and isinstance(receipt.get("job_id"), str)
        }
        if len(members) != len(receipts):
            raise DeletionError(
                409,
                "EXPORT_INCONSISTENT",
                "An exported batch record is incomplete. Deletion was not started.",
                batch_id=row["id"],
            )
        result[str(row["id"])] = members
    return result


def _minimal_allocations(allocations):
    if not isinstance(allocations, list):
        raise DeletionError(
            409,
            "EXPORT_INCONSISTENT",
            "The approved receipt ledger is incomplete. Deletion was not started.",
        )
    result = []
    for allocation in allocations:
        if not isinstance(allocation, dict) or not isinstance(allocation.get("key"), str):
            raise DeletionError(
                409,
                "EXPORT_INCONSISTENT",
                "The approved receipt ledger is incomplete. Deletion was not started.",
            )
        quantity = allocation.get("qty")
        if not isinstance(quantity, (str, int, float)) or isinstance(quantity, bool):
            raise DeletionError(
                409,
                "EXPORT_INCONSISTENT",
                "The approved receipt ledger is incomplete. Deletion was not started.",
            )
        result.append({"key": allocation["key"], "qty": str(quantity)})
    return result


def _upload_paths(store, jobs):
    uploads = (Path(store.root) / "uploads").resolve()
    result = []
    for job in jobs:
        path = Path(job.get("path", "")).resolve()
        if not path.is_relative_to(uploads):
            raise DeletionError(
                409,
                "UPLOAD_PATH_INVALID",
                "A selected invoice has an invalid stored source path. Deletion was not started.",
                job_ids=[job.get("id")],
            )
        result.append(path)
    return list(dict.fromkeys(result))


def _redact_selected_jobs(value, selected_ids):
    """Remove selected-job objects while retaining unrelated aggregate entries."""
    if isinstance(value, dict):
        if value.get("job_id") in selected_ids:
            return None, True
        changed = False
        result = {}
        for key, item in value.items():
            redacted, item_changed = _redact_selected_jobs(item, selected_ids)
            changed = changed or item_changed
            if redacted is not None:
                result[key] = redacted
        return result, changed
    if isinstance(value, list):
        changed = False
        result = []
        for item in value:
            redacted, item_changed = _redact_selected_jobs(item, selected_ids)
            changed = changed or item_changed
            if redacted is not None:
                result.append(redacted)
        return result, changed
    return value, False


def _remove_invoice_audit(connection, selected_ids, jobs):
    file_signatures = {
        (job.get("filename"), job.get("size"))
        for job in jobs
        if job.get("filename") is not None and job.get("size") is not None
    }
    for row in connection.execute(
        "SELECT id,event,payload FROM audit ORDER BY id"
    ).fetchall():
        payload = json.loads(row["payload"])
        payload, references_job = _redact_selected_jobs(payload, selected_ids)
        references_file = False
        had_files = isinstance(payload, dict) and isinstance(payload.get("files"), list)
        if had_files:
            kept_files = []
            for item in payload["files"]:
                selected_file = (
                    isinstance(item, dict)
                    and (item.get("name"), item.get("size")) in file_signatures
                )
                references_file = references_file or selected_file
                if not selected_file:
                    kept_files.append(item)
            payload["files"] = kept_files
        if payload is None or (had_files and references_file and not payload["files"]):
            connection.execute("DELETE FROM audit WHERE id=?", (row["id"],))
        elif references_job or references_file:
            connection.execute(
                "UPDATE audit SET payload=? WHERE id=?",
                (json.dumps(payload, separators=(",", ":")), row["id"]),
            )
