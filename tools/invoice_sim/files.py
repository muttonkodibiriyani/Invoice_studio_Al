"""Files a user might upload, through the real preflight, upload route and readers, AI off. Each one must end in a clear
message or a reviewable invoice: never a crash, never an empty success."""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import time
from pathlib import Path

import openpyxl
import pypdfium2
from PIL import Image
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

from .generator import render_pdf

OPTIONS = {"engine": "auto", "ai_fallback": False, "provider": "openai", "model": "", "language": "en"}
LETTER = ["Dear Sir or Madam,", "We write to confirm the annual maintenance visit next month.", "Please make sure the loading bay is clear.",
          "This letter is not a request for payment.", "Yours faithfully, The Facilities Team"]


def _pdf(writer, **kwargs) -> bytes:
    out = io.BytesIO()
    page = canvas.Canvas(out, **kwargs)
    writer(page)
    page.save()
    return out.getvalue()


def _letter(page):
    for n, line in enumerate(LETTER):
        page.drawString(72, 720 - 22 * n, line)
    page.showPage()


def _join(*sources: bytes) -> bytes:
    target = pypdfium2.PdfDocument.new()
    for source in sources:
        target.import_pages(pypdfium2.PdfDocument(source))
    out = io.BytesIO()
    target.save(out)
    return out.getvalue()


def _image(pdf: bytes, fmt: str, dpi=150) -> bytes:
    picture = pypdfium2.PdfDocument(pdf)[0].render(scale=dpi / 72).to_pil().convert("RGB")
    out = io.BytesIO()
    picture.save(out, fmt, **({"quality": 80} if fmt == "JPEG" else {}))
    return out.getvalue()


def cases(c, folder: Path) -> list[dict]:
    import numpy as np

    from .scan import scan_pdf

    """Name, bytes, what the file is. The invoices come from the corpus, so the expected number is known."""
    rng = random.Random(c.seed)
    folder.mkdir(parents=True, exist_ok=True)
    # One invoice per case: the same number twice would stop at the duplicate guard and hide what the export holds.
    base = [i for i in c.invoices if i.family in ("grid-classic", "ruled-zebra", "plain-whitespace")]
    native = [render_pdf(i, folder / f"{i.id}.pdf").read_bytes() for i in base[:14]]
    scan_path, _ = scan_pdf(folder / f"{base[1].id}.pdf", folder / f"{base[1].id}.scan.pdf", f"{c.seed}:files", "office")
    scanned = Path(scan_path).read_bytes()
    scan_path, _ = scan_pdf(folder / f"{base[13].id}.pdf", folder / f"{base[13].id}.scan.pdf", f"{c.seed}:files-pdf", "office")
    scanned_alone = Path(scan_path).read_bytes()
    blank = _pdf(lambda p: p.showPage())
    noise = np.random.default_rng(c.seed).integers(0, 255, (2200, 1700, 3), dtype=np.uint8)
    noise_out = io.BytesIO()
    Image.fromarray(noise).save(noise_out, "PNG")
    number = [i.truth["number"] for i in base[:14]]
    net = [str(i.truth["net"]) for i in base[:14]]
    heavy = np.random.default_rng(c.seed + 1).integers(0, 255, (1950, 1800, 3), dtype=np.uint8)
    heavy_out = io.BytesIO()
    Image.fromarray(heavy).save(heavy_out, "PNG")
    pad = bytes(rng.getrandbits(8) for _ in range(1024)) * 1024 * 13
    return [
        {"case": "PDF native", "name": "invoice.pdf", "content": native[12], "number": number[12]},
        {"case": "PDF scanned", "name": "invoice-scan.pdf", "content": scanned_alone, "number": number[13]},
        {"case": "JPG", "name": "invoice.jpg", "content": _image(native[2], "JPEG"), "number": number[2]},
        {"case": "PNG", "name": "invoice.png", "content": _image(native[3], "PNG"), "number": number[3]},
        {"case": "TIFF", "name": "invoice.tiff", "content": _image(native[4], "TIFF"), "number": number[4]},
        {"case": "WEBP", "name": "invoice.webp", "content": _image(native[5], "WEBP"), "number": number[5]},
        {"case": "HEIC (name and header only, no encoder here)", "name": "invoice.heic", "content": b"\x00\x00\x00\x18ftypheic" + b"\x00" * 2000},
        {"case": "three invoices in one PDF", "name": "three.pdf", "content": _join(*native[6:9]), "numbers": number[6:9], "nets": net[6:9]},
        {"case": "two invoices in one PDF, both numbers readable alone", "name": "two.pdf", "content": _join(native[9], native[10]), "numbers": number[9:11], "nets": net[9:11]},
        {"case": "native and scanned pages mixed", "name": "mixed.pdf", "content": _join(native[0], scanned), "numbers": number[:2], "nets": net[:2]},
        {"case": "blank PDF", "name": "blank.pdf", "content": blank},
        {"case": "invoice then a blank page", "name": "trailing-blank.pdf", "content": _join(native[11], blank), "number": number[11]},
        {"case": "password-protected PDF", "name": "locked.pdf", "content": _pdf(_letter, encrypt=StandardEncryption("sim-secret", canPrint=0))},
        {"case": "corrupt PDF, garbage after the header", "name": "corrupt.pdf", "content": b"%PDF-1.7\n" + bytes(rng.getrandbits(8) for _ in range(4000))},
        {"case": "truncated PDF", "name": "truncated.pdf", "content": native[0][: len(native[0]) // 2]},
        {"case": "corrupt PNG", "name": "corrupt.png", "content": b"\x89PNG\r\n\x1a\n" + bytes(rng.getrandbits(8) for _ in range(4000))},
        {"case": "a PDF that is not one", "name": "fake.pdf", "content": b"this is not a pdf at all"},
        {"case": "empty file", "name": "empty.pdf", "content": b""},
        {"case": "over 12 MB", "name": "huge.pdf", "content": b"%PDF-1.7\n" + pad},
        {"case": "under the limit, about 10 MB image", "name": "heavy.png", "content": heavy_out.getvalue()},
        {"case": "not an invoice: a letter", "name": "letter.pdf", "content": _pdf(_letter)},
        {"case": "not an invoice: a noise photo", "name": "photo.png", "content": noise_out.getvalue()},
        {"case": "not an invoice: plain text", "name": "notes.txt", "content": "\n".join(LETTER).encode()},
        {"case": "not an invoice: spreadsheet bytes named xlsx", "name": "book.xlsx", "content": b"PK\x03\x04not really a workbook"},
    ]


def verdict(rec: dict) -> str:
    """Refused: told at upload. Failed: the job says why. Read: invoice values to review. Empty success: nothing read, nothing said.
    Invoices lost: a file holding several became fewer records and nothing tells the user. The harness's own sign-in is no verdict on the app."""
    if rec.get("server_error"):
        return "CRASH"
    if rec["upload_status"] == 401:
        return "NOT RUN, SIGN-IN EXPIRED"
    if rec["upload_status"] != 200:
        return "refused"
    outcomes = rec.get("jobs") or []
    if any(j["status"] == "timed out" for j in outcomes):
        return "STUCK"
    if any(j["status"] == "job not found" for j in outcomes):
        return "UPLOADED, JOB NOT FOUND"
    if all(j["status"] == "error" for j in outcomes):
        return "failed with message" if all(j.get("error") for j in outcomes) else "FAILED WITH NO MESSAGE"
    if len(outcomes) < rec.get("invoices_in_file", 0) and not any(re.search(r"(several|multiple|more than one|other|two|three) invoices?\b", str(j.get("told")), re.I) for j in outcomes):
        return "INVOICES LOST, NOT TOLD"
    if any(e["status"] >= 500 for e in rec.get("export") or []):
        return "EXPORT CRASH"
    empty = [j for j in outcomes if not j.get("number") and not j.get("lines")]
    if any(not j.get("told") for j in empty):
        return "EMPTY SUCCESS"
    return "nothing read, told so" if empty else "read, review"


def settle(rig, jid, deadline):
    job = {}
    while time.monotonic() < deadline:
        response = rig.client.get(f"/api/jobs/{jid}", headers=rig.headers)
        if response.status_code != 200:
            return {"status": "job not found", "error": f"{response.status_code} {response.text[:200]}"}
        job = response.json()
        if job.get("status") not in ("queued", "processing"):
            return job
        time.sleep(1)
    return {**job, "status": "timed out"}


def summary(job: dict) -> dict:
    invoice = job.get("invoice") or {}
    validation = job.get("validation") or {}
    rules = job.get("rules") or {}
    issues = rules.get("issues") or validation.get("issues") or []
    return {"filename": job.get("filename"), "status": job.get("status"), "error": job.get("error"), "note": job.get("extraction_note"),
            "number": invoice.get("number"), "lines": len(invoice.get("lines") or []), "net": invoice.get("net"),
            "gaps": (job.get("readers") or {}).get("gaps"), "document_type": job.get("document_type_hint"),
            "told": [*filter(None, [job.get("extraction_note")]), *(str(i.get("message"))[:120] for i in issues)]}


def exported(rig, jid) -> dict:
    job = rig.client.get(f"/api/jobs/{jid}", headers=rig.headers).json()
    response = rig.client.post(f"/api/jobs/{jid}/export", headers=rig.headers, json={"revision": job["revision"]})
    if response.status_code != 200:
        return {"status": response.status_code, "detail": response.json().get("detail")}
    book = openpyxl.load_workbook(io.BytesIO(rig.client.get(f"/api/exports/{response.json()['id']}", headers=rig.headers).content))
    return {"status": 200, "details_rows": book["Details"].max_row - 1, "document": book["Header"]["B2"].value}


def files(args):
    from .run import RulesRig, corpus
    out = Path(args.out)
    c = corpus(args.seed)
    rig = RulesRig(c, out / "files-app" / str(os.getpid()))
    results = []
    only = tuple(args.only.split(",")) if args.only else ()
    for case in cases(c, out / "files-src"):
        if only and not case["case"].startswith(only):
            continue
        rig.sign_in()
        name, content = case["name"], case["content"]
        rec = {"case": case["case"], "name": name, "size": len(content)}
        planned = rig.client.post("/api/preflight", headers=rig.headers, json={"options": OPTIONS, "files": [{"name": name, "size": max(len(content), 1)}]})
        rec["preflight"] = planned.status_code
        if planned.status_code != 200:
            rec.update(upload_status=planned.status_code, message=planned.json().get("detail"))
        else:
            token = planned.json()["token"]
            rig.client.post("/api/preflight/confirm", headers=rig.headers, json={"token": token})
            response = rig.client.post("/api/invoices", headers=rig.headers, files={"file": (name, content, "application/octet-stream")},
                                       data={"options": json.dumps(OPTIONS), "preflight_token": token})
            rec["upload_status"] = response.status_code
            rec["server_error"] = response.status_code >= 500
            body = response.json() if "json" in response.headers.get("content-type", "") else {}
            if response.status_code != 200:
                rec["message"] = body.get("detail") or response.text[:200]
            else:
                # An upload the app splits answers with one part; the others are found by the upload they came from.
                sha = hashlib.sha256(content).hexdigest()
                listed = rig.client.get("/api/state", headers=rig.headers).json()["jobs"]
                parts = [j["id"] for j in listed if (j.get("split") or {}).get("source_sha256") == sha]
                ids = parts or [j["id"] for j in body.get("jobs", [])] or [body.get("id")]
                deadline = time.monotonic() + 900
                jobs = [settle(rig, jid, deadline) for jid in ids]
                rec["jobs"] = [summary(j) for j in jobs]
                rec["refused_note"] = body.get("refusal") or body.get("message")
                rec["export"] = [exported(rig, j["id"]) for j in jobs if j.get("status") not in ("error", "timed out", "job not found")]
                read = [j["number"] for j in rec["jobs"] if j["number"]]
                if case.get("number"):
                    rec["number_right"] = read == [case["number"]]
                if case.get("numbers"):
                    rec["numbers_right"] = sorted(read) == sorted(case["numbers"])
                    rec["invoices_in_file"] = len(case["numbers"])
                    rec["read_values_are_of"] = [{"number": case["numbers"].index(j["number"]) if j["number"] in case["numbers"] else None,
                                                  "net": next((k for k, v in enumerate(case["nets"]) if j["net"] and float(j["net"]) == float(v)), None)} for j in rec["jobs"]]
        rec["verdict"] = verdict(rec)
        results.append(rec)
        (out / "files.json").write_text(json.dumps(results, indent=1, default=str))
        print(rec["verdict"], "|", rec["case"], "|", rec.get("message") or [j["status"] for j in rec.get("jobs", [])], flush=True)
