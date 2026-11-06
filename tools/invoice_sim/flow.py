"""Part B: the whole flow on a real server process, PostgreSQL and blob storage: upload, read, review with typed values, export, open the
workbook, compare with the truth. Also several at once, the server killed mid-job, a duplicate upload, re-export after lock.

    INV_STUDIO_DATABASE_URL=... INV_STUDIO_AZURE_STORAGE_CONNECTION_STRING=... python -m tools.invoice_sim.flow --out DIR --port N
"""
from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import jwt
import openpyxl
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .generator import SEED, render_pdf
from .master import LOCATION, master_workbook, parent_id, rules_config, site_of
from .run import ITEM_CELL, corpus, score_items
from .score import align

OPTIONS = {"engine": "auto", "ai_fallback": False, "provider": "openai", "model": "", "language": "en"}
KID = "sim-kid"
SETTLE = 1500


def _key(out: Path):
    path = out / "signing.pem"
    if not path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        path.chmod(0o600)
    return serialization.load_pem_private_key(path.read_bytes(), None)


def serve(args):
    """The server, in its own process so the flow can kill it. Only the tenant's signing key is local: everything else is the app."""
    import uvicorn

    from app.authentication import EntraIdentity
    from app.cloud_store import PostgresStore
    from app.main import create_app
    from tests.conftest import CLIENT_ID, TENANT_ID

    os.environ.update(INV_STUDIO_CLOUD="1", INV_STUDIO_ENTRA_TENANT_ID=TENANT_ID, INV_STUDIO_ENTRA_CLIENT_ID=CLIENT_ID)
    public = _key(Path(args.out)).public_key()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public))
    identity = EntraIdentity(fetch=lambda _url: {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]})
    app = create_app(Path(args.out) / "app-data", identity=identity, store_type=PostgresStore)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


class Server:
    def __init__(self, out: Path, port: int):
        self.out, self.port, self.process = out, port, None

    def start(self):
        log = open(self.out / "server.log", "ab")
        self.process = subprocess.Popen([sys.executable, "-m", "tools.invoice_sim.flow", "--serve", "--out", str(self.out), "--port", str(self.port)],
                                        stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            # A server left on the port would answer in this one's place: the process has to be alive, not just the port.
            if self.process.poll() is not None:
                raise RuntimeError("the server exited; see server.log")
            try:
                httpx.get(f"http://127.0.0.1:{self.port}/api/session", timeout=3)
                return
            except httpx.TransportError:
                time.sleep(1)
        raise RuntimeError("the server did not start; see server.log")

    def kill(self):
        """As a crash: nothing gets to clean up, and the reader children go too."""
        os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait()


class Api:
    def __init__(self, out: Path, port: int):
        from tests.conftest import CLIENT_ID, ISSUER, OWNER, TENANT_ID
        now = int(time.time())
        token = jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "tid": TENANT_ID, "iat": now, "nbf": now, "exp": now + 86400, "ver": "2.0", "scp": "access_as_user",
                            "azp": CLIENT_ID, "oid": "owner-uid", "preferred_username": OWNER, "name": "owner", "roles": ["admin"]},
                           _key(out), algorithm="RS256", headers={"kid": KID})
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {token}", "X-Studio-Request": "1"}, timeout=180)
        self.busy = []

    def upload(self, name, content):
        plan = self.http.post("/api/preflight", json={"options": OPTIONS, "files": [{"name": name, "size": len(content)}]})
        plan.raise_for_status()
        token = plan.json()["token"]
        self.http.post("/api/preflight/confirm", json={"token": token}).raise_for_status()
        # The browser waits and retries a 429 (upload code in app.js), so this does the same and counts them.
        while True:
            response = self.http.post("/api/invoices", files={"file": (name, content, "application/pdf")}, data={"options": json.dumps(OPTIONS), "preflight_token": token})
            if response.status_code != 429:
                return response
            self.busy.append(response.json().get("detail"))
            time.sleep(float(response.headers.get("retry-after") or 5))

    def job(self, jid):
        return self.http.get(f"/api/jobs/{jid}").json()

    def settle(self, jid, seconds=SETTLE):
        job = {}
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                job = self.job(jid)
            except httpx.TransportError:
                time.sleep(2)
                continue
            if job.get("status") not in ("queued", "processing"):
                return job
            time.sleep(2)
        return {**job, "status": "timed out"}


def _typed_invoice(truth):
    lines = [{k: line.get(k) for k in ("sku", "gtin", "description", "uom", "qty", "price", "net_amount")} for line in truth["lines"]]
    return {"number": truth["number"], "supplier_name": truth["supplier_name"], "date": truth["date"], "date_printed": truth["date_printed"], "po": truth["po"],
            "currency": truth["currency"], "net": truth["net"], "tax": truth["tax"], "lines": lines}


def typed_entries(truth, tax_code="SIMV5", items=True):
    lines = {}
    for n, line in enumerate(truth["lines"], 1):
        row = {"Unit Tax Code": tax_code}
        if items and line["item_id"]:
            row["Item"] = parent_id(line["item_id"])
        lines[str(n)] = row
    return {"header": {"location": LOCATION, "location_type": "Store (S)", "taxCode": tax_code}, "lines": lines}


def review_typed(api, job, truth, items=True):
    return api.http.post(f"/api/jobs/{job['id']}/review", json={"invoice": _typed_invoice(truth), "revision": job["revision"], "confirm": True, "entries": typed_entries(truth, items=items)})


def workbook_rows(content: bytes) -> dict:
    book = openpyxl.load_workbook(io.BytesIO(content))
    out = {}
    for sheet in book:
        rows = [[c.value for c in row] for row in sheet.iter_rows()]
        out[sheet.title] = [dict(zip(rows[0], row)) for row in rows[1:]] if rows else []
    return out


def _equal(got, want) -> bool:
    if got in (None, "") or want in (None, ""):
        return got in (None, "") and want in (None, "")
    try:
        return float(got) == float(want)
    except (TypeError, ValueError):
        return str(got).strip() == str(want).strip()


def compare(sheets: dict, truth: dict, site: str) -> dict:
    """Every compared cell of the workbook against the truth. A mismatch lists the sheet, the column and both values.
    UPC is left empty by the app's default target (app/excel.py), so the truth for that column here is blank."""
    wrong, cells = [], 0
    header = (sheets.get("Header") or [{}])[0]
    want_header = {"Document": truth["number"], "Supplier Site": site, "Order No": truth["po"], "Location": LOCATION, "Location Type": "Store (S)",
                   "Document Date": truth["date"], "Total Cost Ex Tax": truth["net"], "Tax Amount": truth["tax"]}
    for column, want in want_header.items():
        cells += 1
        got = header.get(column)
        got = str(got)[:10] if column == "Document Date" and got else got
        if not _equal(got, want):
            wrong.append({"sheet": "Header", "column": column, "truth": want, "got": got})
    details = sheets.get("Details") or []
    if len(details) != len(truth["lines"]):
        wrong.append({"sheet": "Details", "column": "row count", "truth": len(truth["lines"]), "got": len(details)})
    for n, (row, line) in enumerate(zip(details, truth["lines"]), 1):
        item = parent_id(line["item_id"]) if line["item_id"] else None
        for column, want in (("Item", item), ("UPC", None), ("Unit Cost", line["price"]), ("Quantity", line["qty"])):
            cells += 1
            if not _equal(row.get(column), want):
                wrong.append({"sheet": "Details", "line": n, "class": line["item_class"], "column": column, "truth": want, "got": row.get(column)})
    return {"cells": cells, "wrong": wrong}


def item_counts(job, sheets, truth):
    """Item as the app chose it, per item class, from the exported Details sheet: the scorer part A uses, on the workbook itself."""
    read = (job.get("invoice") or {}).get("lines") or []
    pairs, dropped, phantom = align(read, truth["lines"])
    items, columns, wrong = score_items(truth, read, pairs, phantom, job.get("rules") or {}, sheets.get("Details") or [])
    for j in dropped:
        items.setdefault(truth["lines"][j]["item_class"], dict(ITEM_CELL))["row not read"] += 1
    return {"items": items, "columns": columns, "wrong_items": wrong}


def add_counts(total, counts):
    for cls, cell in counts["items"].items():
        into = total.setdefault(cls, dict(ITEM_CELL))
        for k, v in cell.items():
            into[k] += v
    return total


def export(api, jid):
    job = api.job(jid)
    response = api.http.post(f"/api/jobs/{jid}/export", json={"revision": job["revision"]})
    if response.status_code != 200:
        return {"status": response.status_code, "detail": response.json().get("detail")}
    eid = response.json()["id"]
    file = api.http.get(f"/api/exports/{eid}")
    return {"status": 200, "id": eid, "content": file.content, "file_status": file.status_code}


def full_invoice(api, invoice, folder, note=None):
    """Upload, read, review with typed values, export, open the workbook, compare."""
    path = render_pdf(invoice, folder / f"{invoice.id}.pdf")
    uploaded = api.upload(path.name, path.read_bytes())
    uploaded.raise_for_status()
    jid = uploaded.json()["id"]
    job = api.settle(jid)
    rec = {"invoice": invoice.id, "job": jid, "read_status": job.get("status"), "read_number": (job.get("invoice") or {}).get("number"), "error": job.get("error")}
    if job.get("status") in ("error", "timed out"):
        return rec
    truth = json.loads(json.dumps(invoice.truth, default=str))
    reviewed = review_typed(api, job, truth)
    rec["review"] = reviewed.status_code
    if reviewed.status_code != 200:
        rec["review_detail"] = reviewed.text[:400]
        return rec
    made = export(api, jid)
    rec["export"] = made["status"]
    if made["status"] != 200:
        rec["export_detail"] = made.get("detail")
        return rec
    rec.update(export_id=made["id"], compare=compare(workbook_rows(made["content"]), truth, _site(invoice)))
    return rec


def _site(invoice):
    return site_of(invoice.truth["supplier_name"], invoice.style["totals_values"]["code"])["supplier_site"]


def app_items(api, invoice, folder, reviewed):
    """No Item typed. reviewed: the reader's lines replaced by typed ones, so only matching is on trial. Otherwise exactly what was read."""
    path = render_pdf(invoice, folder / f"{invoice.id}.pdf")
    uploaded = api.upload(path.name, path.read_bytes())
    uploaded.raise_for_status()
    job = api.settle(uploaded.json()["id"])
    truth = json.loads(json.dumps(invoice.truth, default=str))
    if job.get("status") in ("error", "timed out"):
        return {"invoice": invoice.id, "status": job.get("status"), "error": job.get("error")}
    if reviewed:
        response = review_typed(api, job, truth, items=False)
        if response.status_code != 200:
            return {"invoice": invoice.id, "review": response.status_code, "detail": response.text[:300]}
    made = export(api, job["id"])
    if made["status"] != 200:
        return {"invoice": invoice.id, "export": made["status"], "detail": made.get("detail")}
    sheets = workbook_rows(made["content"])
    return {"invoice": invoice.id, "rules_status": (api.job(job["id"]).get("rules") or {}).get("status"), **item_counts(api.job(job["id"]), sheets, truth)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--serve", action="store_true")
    p.add_argument("--out", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--only", default="")
    p.add_argument("--seed", type=int, default=SEED)
    args = p.parse_args()
    if args.serve:
        return serve(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    folder = out / "pdfs"
    folder.mkdir(exist_ok=True)
    c = corpus(args.seed)
    by = {i.id: i for i in c.invoices}
    server = Server(out, args.port)
    server.start()
    try:
        drive(args, out, folder, c, by, server)
    finally:
        if server.process.poll() is None:
            server.kill()


def drive(args, out, folder, c, by, server):
    api = Api(out, args.port)
    assert api.http.get("/api/session").status_code == 200
    report = {}

    def save(name, value):
        report[name] = value
        (out / "flow.json").write_text(json.dumps(report, indent=1, default=str))
        print("==", name, json.dumps(value, default=str)[:3000], flush=True)

    upload = api.http.post("/api/item-master/validate", files={"file": ("master.xlsx", master_workbook(c))})
    upload.raise_for_status()
    api.http.post(f"/api/item-master/uploads/{upload.json()['upload_id']}/activate").raise_for_status()
    api.http.post("/api/fine-rules/config", json=rules_config(c)).raise_for_status()

    first = full_invoice(api, by["grid-classic-01"], folder)
    save("one_invoice", first)
    if args.only == "one":
        return

    names = ["grid-classic-03", "boxed-header-00", "ruled-zebra-03", "plain-whitespace-02", "inline-header-03", "buyer-first-01"]
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        together = list(pool.map(lambda n: full_invoice(api, by[n], folder), names))
    mixed = [r["invoice"] for r in together if r.get("compare") and any(w["column"] in ("Document", "row count") for w in r["compare"]["wrong"])]
    save("six_at_once", {"results": together, "documents_crossed": mixed, "busy_answers": len(api.busy), "busy_message": sorted(set(api.busy))})
    done = [r["job"] for r in together if r.get("export") == 200]
    batch = api.http.post("/api/exports/batch", json={"job_ids": done})
    save("batch_export", {"status": batch.status_code, "detail": batch.text[:200] if batch.status_code != 200 else "ok", "type": batch.headers.get("content-type")})

    for reviewed, label in ((True, "items_lines_typed"), (False, "items_as_read")):
        chosen = ["grid-classic-00", "boxed-header-02", "ruled-zebra-00", "plain-whitespace-00"] if reviewed else ["grid-classic-02", "boxed-header-03", "ruled-zebra-01", "plain-whitespace-01"]
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            found = list(pool.map(lambda n: app_items(api, by[n], folder, reviewed), chosen))
        total = {}
        for one in found:
            if "items" in one:
                add_counts(total, one)
        save(label, {"per_class": total, "wrong_items": [dict(w, invoice=r["invoice"]) for r in found for w in r.get("wrong_items", [])], "results": [{k: r.get(k) for k in ("invoice", "rules_status", "export", "detail", "status", "error")} for r in found]})

    victims = [by["grid-classic-04"], by["boxed-header-01"]]
    ids, truths = {}, {}
    for inv in victims:
        path = render_pdf(inv, folder / f"{inv.id}.pdf")
        ids[inv.id] = api.upload(path.name, path.read_bytes()).json()["id"]
        truths[inv.id] = json.loads(json.dumps(inv.truth, default=str))
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and not any(api.job(j).get("status") == "processing" for j in ids.values()):
        time.sleep(1)
    at_kill = {k: api.job(j).get("status") for k, j in ids.items()}
    server.kill()
    down = time.monotonic()
    server.start()
    api = Api(out, args.port)
    after = {}
    for name, jid in ids.items():
        job = api.settle(jid)
        rec = {"status": job.get("status"), "error": job.get("error"), "takeovers": job.get("takeovers"), "number": (job.get("invoice") or {}).get("number"), "seconds_after_restart": round(time.monotonic() - down)}
        if job.get("status") not in ("error", "timed out"):
            rec["review"] = review_typed(api, job, truths[name]).status_code
            made = export(api, jid)
            rec["export"] = made["status"]
            if made["status"] == 200:
                rec["wrong"] = compare(workbook_rows(made["content"]), truths[name], _site(by[name]))["wrong"]
        after[name] = rec
    state = api.http.get("/api/state").json()["jobs"]
    save("restart", {"status_at_kill": at_kill, "after": after, "jobs_with_these_names": sum(j["filename"] in {f"{n}.pdf" for n in ids} for j in state)})

    path = folder / "grid-classic-01.pdf"
    again = api.upload(path.name, path.read_bytes())
    rec = {"upload": again.status_code, "detail": again.text[:240] if again.status_code != 200 else None}
    if again.status_code == 200:
        job = api.settle(again.json()["id"])
        rec.update(status=job.get("status"), duplicate=[i["message"] for i in (job.get("validation") or {}).get("issues", []) if i.get("code") == "Duplicate"], job=job["id"])
        truth = json.loads(json.dumps(by["grid-classic-01"].truth, default=str))
        rec["review"] = review_typed(api, job, truth).status_code
        made = export(api, job["id"])
        rec.update(export=made["status"], export_detail=made.get("detail"))
    save("duplicate", rec)

    if first.get("export") == 200:
        jid = first["job"]
        a = export(api, jid)
        b = export(api, jid)
        job = api.job(jid)
        edit = api.http.post(f"/api/jobs/{jid}/review", json={"invoice": {**job["invoice"], "net": "1.00"}, "revision": job["revision"]})
        save("re_export_after_lock", {"same_export": a.get("id") == b.get("id") == first["export_id"], "same_bytes": a.get("content") == b.get("content"), "edit_after_export": edit.status_code, "edit_detail": edit.text[:200]})

    letter = out / "letter.pdf"
    from .files import _letter, _pdf
    letter.write_bytes(_pdf(_letter))
    held = api.settle(api.upload("letter.pdf", letter.read_bytes()).json()["id"])
    made = export(api, held["id"])
    cells = workbook_rows(made["content"]) if made.get("content") else {}
    save("unreviewed_empty_export", {"status": held.get("status"), "export": made["status"], "detail": made.get("detail"), "details_rows": len(cells.get("Details") or []), "header": (cells.get("Header") or [{}])[0]})


if __name__ == "__main__":
    main()
