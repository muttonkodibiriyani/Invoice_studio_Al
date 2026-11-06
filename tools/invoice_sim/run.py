"""The full simulation, by hand. Real readers, AI off, no network. Results stay in --out, outside git.

    python -m tools.invoice_sim.run build --out /tmp/sim
    python -m tools.invoice_sim.run read  --out /tmp/sim --only carbon,ink --workers 4
    python -m tools.invoice_sim.run score --out /tmp/sim
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("INV_STUDIO_LEARN", "0")
ROOT = Path(__file__).resolve().parents[2]
PER_FAMILY = 5
ITEM_CELL = {"correct": 0, "blank": 0, "WRONG": 0, "silent": 0, "row not read": 0}


def corpus(seed):
    from .degrade import DEGRADED
    from .generator import build_corpus
    return build_corpus(seed, PER_FAMILY, tuple(DEGRADED), PER_FAMILY)


def build(args):
    from .degrade import DEGRADED, render_degraded
    from .generator import render_pdf
    from .scan import scan_pdf
    out = Path(args.out)
    docs = out / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    manifest = []
    c = corpus(args.seed)
    for inv in c.invoices:
        truth = inv.truth
        if inv.family in DEGRADED:
            path = render_degraded(inv, docs / inv.id)
            manifest.append({"doc": f"{inv.id}.asis", "family": inv.family, "variant": "asis", "path": str(path)})
        else:
            native = render_pdf(inv, docs / f"{inv.id}.native.pdf")
            scanned, profile = scan_pdf(native, docs / f"{inv.id}.scan.pdf", f"{args.seed}:{inv.id}")
            manifest.append({"doc": f"{inv.id}.native", "family": inv.family, "variant": "native", "path": str(native)})
            manifest.append({"doc": f"{inv.id}.scan", "family": inv.family, "variant": "scan", "profile": profile, "path": str(scanned)})
        (docs / f"{inv.id}.truth.json").write_text(json.dumps({"id": inv.id, **truth}, default=str))
    for group, folder in (("hard-cases", ROOT / "hard-cases"), ("samples", ROOT / "samples")):
        for answer in sorted(folder.glob("*.json")):
            found = [p for p in folder.glob(answer.stem + ".*") if p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")]
            if found:
                shutil.copy(answer, docs / f"{group}__{answer.stem}.truth.json")
                manifest.append({"doc": f"{group}__{answer.stem}.asis", "family": group, "variant": "repo", "path": str(found[0])})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=0))
    print(len(c.invoices), "invoices", len(manifest), "documents", len(c.master), "master rows")


def _read_one(job):
    doc, out = job
    from app import engines
    from app.models import ProcessingOptions
    target = Path(out) / "reads" / f"{doc['doc']}.json"
    if target.exists():
        return doc["doc"]
    root = Path(out) / "work" / str(os.getpid())
    (root / "work").mkdir(parents=True, exist_ok=True)
    (root / "templates").mkdir(exist_ok=True)

    def no_ai(*_):
        raise AssertionError("The simulation runs with the AI off")

    started = time.monotonic()
    try:
        result = engines.process(Path(doc["path"]), ProcessingOptions(ai_fallback=False), SimpleNamespace(root=root), no_ai)
    except Exception as error:
        result = {"crash": type(error).__name__, "message": str(error)[:300]}
    result["seconds"] = round(time.monotonic() - started, 1)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, default=str))
    tmp.replace(target)
    return doc["doc"]


def read(args):
    out = Path(args.out)
    (out / "reads").mkdir(exist_ok=True)
    manifest = json.loads((out / "manifest.json").read_text())
    only = tuple(args.only.split(",")) if args.only else ()
    wanted = [d for d in manifest if (not only or d["doc"].startswith(only)) and (not args.variant or d["variant"] == args.variant)]
    wanted = [d for d in wanted if not (out / "reads" / f"{d['doc']}.json").exists()]
    print("to read:", len(wanted), flush=True)
    os.nice(5)
    with multiprocessing.get_context("spawn").Pool(args.workers, maxtasksperchild=8) as pool:
        for n, name in enumerate(pool.imap_unordered(_read_one, [(d, str(out)) for d in wanted]), 1):
            print(n, name, time.strftime("%H:%M:%S"), flush=True)


def _plain(value):
    # The JSON form the app hands to rules_view: create_app keeps its own copy of this inside the closure.
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, set):
        return sorted(_plain(v) for v in value)
    return value if isinstance(value, (int, float, str, bool)) or value is None else str(value)


class RulesRig:
    """The real rule chain and export workbook on a master activated through the real API."""

    def __init__(self, c, data_dir):
        import pytest
        from fastapi.testclient import TestClient

        from app.authentication import EntraIdentity
        from app.fine_rules import RulesConfig
        from app.fine_rules_source import LookupRulesSource
        from app.main import create_app
        from app.store import Store
        from tests.conftest import OWNER, Tenant, bearer, entra_env

        from .master import master_workbook, rules_config
        self.patch = pytest.MonkeyPatch()
        entra_env(self.patch)
        tenant = Tenant()


        class LocalBlobStore(Store):
            # Entra identity means the cloud upload path. Part A does not test blob storage, so files stay on local disk.
            def persist_blob(self, path):
                pass

            def ensure_blob(self, path):
                pass

            def remove_blob(self, path):
                pass

        self.app = create_app(Path(data_dir), identity=EntraIdentity(fetch=tenant.jwks), store_type=LocalBlobStore)
        self.client = TestClient(self.app)
        self.client.__enter__()
        headers = bearer(tenant.token("owner-uid", OWNER, roles=("admin",)))
        self.headers = headers
        assert self.client.get("/api/session", headers=headers).status_code == 200
        upload = self.client.post("/api/item-master/validate", headers=headers, files={"file": ("master.xlsx", master_workbook(c))})
        assert upload.status_code == 200, upload.text[:400]
        done = self.client.post(f"/api/item-master/uploads/{upload.json()['upload_id']}/activate", headers=headers)
        assert done.status_code == 200, done.text[:400]
        saved = self.client.post("/api/fine-rules/config", headers=headers, json=rules_config(c))
        assert saved.status_code == 200, saved.text[:400]
        self.source = LookupRulesSource(self.app.state.store)
        self.config = RulesConfig.from_dict(rules_config(c))

    def close(self):
        self.client.__exit__(None, None, None)
        self.patch.undo()

    def run(self, invoice_dict, supplier, text, boxes, evidence=None, ocr_lines=False):
        """As a reviewer who confirmed the printed supplier name: the view on screen and the Details rows of the export."""
        import io

        import openpyxl

        from app.excel import rules_workbook
        from app.fine_rules import run_invoice
        from app.matching import rules_view
        from app.models import Invoice
        invoice = Invoice.model_validate({**invoice_dict, "supplier_name": supplier})
        result = run_invoice(invoice, self.source, self.config, filename="sim.pdf", text_value=text, boxes=boxes,
                             ocr_lines=ocr_lines)
        view = rules_view(_plain(result), text, boxes, evidence, None, None)
        content, _ = rules_workbook([{**view, "owner_accepted": []}], "barcode", blanks=True)
        book = openpyxl.load_workbook(io.BytesIO(content))
        sheet = book["Details"]
        heads = [c.value for c in sheet[1]]
        rows = [dict(zip(heads, [c.value for c in row])) for row in sheet.iter_rows(min_row=2)]
        return view, rows


def _item_verdict(row, truth_line):
    from .master import parent_id
    got = "" if row is None or row.get("Item") in (None, "") else str(row["Item"])
    want = parent_id(truth_line["item_id"]) if truth_line and truth_line.get("item_id") else ""
    if not got:
        return "correct" if not want else "blank"
    return "correct" if got == want else "WRONG"


def score_items(truth, read_lines, pairs, phantom, view, rows):
    """Item, UPC, Unit Cost and Quantity in the exported Details sheet, per item class. Phantom rows have no class of their own."""
    from .score import number
    items, columns, wrong = defaultdict(lambda: dict(ITEM_CELL)), defaultdict(lambda: {"correct": 0, "blank": 0, "WRONG": 0}), []
    view_lines = view.get("lines") or []
    by_read = dict(pairs)
    truth_lines = truth["lines"]
    for i, row in enumerate(rows):
        j = by_read.get(i)
        line = truth_lines[j] if j is not None else None
        cls = line["item_class"] if line else "phantom_row"
        verdict = _item_verdict(row, line)
        cell = view_lines[i].get("cells", {}).get("Item", {}) if i < len(view_lines) else {}
        flagged = bool(cell.get("flagged")) or any(n.get("line") == i + 1 and n.get("code") == "Item Exception" for n in view.get("issues") or [])
        items[cls][verdict] += 1
        if verdict == "WRONG":
            items[cls]["silent"] += 0 if flagged else 1
            wrong.append({"class": cls, "line": i + 1, "truth_item": line and line.get("item_id"), "picked": row.get("Item"), "method": view_lines[i].get("match_method") if i < len(view_lines) else "",
                          "sku_read": read_lines[i].get("sku") if i < len(read_lines) else None, "gtin_read": read_lines[i].get("gtin") if i < len(read_lines) else None, "flagged": flagged})
        if line:
            expect = {"UPC": line.get("gtin"), "Quantity": line.get("qty"), "Unit Cost": line.get("price")}
            for name, want in expect.items():
                got = row.get(name)
                if got in (None, ""):
                    columns[name]["correct" if want in (None, "") else "blank"] += 1
                elif name == "UPC":
                    columns[name]["correct" if str(got).lstrip("0") == str(want or "").lstrip("0") else "WRONG"] += 1
                else:
                    exact = number(want) is not None and number(str(got)) == number(want)
                    columns[name]["correct" if exact else "WRONG"] += 1
    return dict(items), dict(columns), wrong


def _truth_invoice(truth):
    fields = ("number", "supplier_name", "date", "date_printed", "po", "currency", "net", "tax")
    lines = [{k: line.get(k) for k in ("sku", "gtin", "description", "uom", "qty", "price", "net_amount")} for line in truth["lines"]]
    return {**{k: truth.get(k) for k in fields}, "lines": lines}


def score(args):
    from app.fine_rules import ocr_read

    from .score import HEADER_FIELDS, LINE_FIELDS, new_issues, score_read
    out = Path(args.out)
    manifest = json.loads((out / "manifest.json").read_text())
    c = corpus(args.seed)
    by_id = {inv.id: inv for inv in c.invoices}
    rig = RulesRig(c, out / "rig" / str(os.getpid()))
    results = []
    for doc in manifest:
        path = out / "reads" / f"{doc['doc']}.json"
        if not path.exists():
            continue
        result = json.loads(path.read_text())
        stem = doc["doc"].rsplit(".", 1)[0]
        truth = json.loads((out / "docs" / f"{stem}.truth.json").read_text())
        inv = by_id.get(stem)
        entry = {**{k: doc[k] for k in ("doc", "family", "variant")}, "profile": doc.get("profile"), "seconds": result.get("seconds"), "note": result.get("extraction_note")}
        if result.get("crash"):
            entry.update(crash=result["crash"], message=result.get("message"))
            results.append(entry)
            continue
        view, rows, baseline = None, [], None
        rules_error = None
        if inv and result.get("invoice") is not None:
            try:
                ocr = ocr_read(result)
                text, boxes = result.get("text") or "", result.get("boxes") or []
                view, rows = rig.run(result["invoice"], truth["supplier_name"], text, boxes, result.get("evidence"), ocr)
                baseline, _ = rig.run(_truth_invoice(truth), truth["supplier_name"], text, boxes, result.get("evidence"), ocr)
            except Exception as error:
                rules_error = f"{type(error).__name__}: {str(error)[:200]}"
        scored = score_read(truth, result, view, baseline)
        supplier = scored["fields"]["supplier_name"]
        if view is not None and supplier["verdict"] == "WRONG" and not supplier["flagged"] and result["invoice"].get("supplier_name"):
            # The rows above assume a reviewer confirmed the supplier. Here the name stays as read: does the app notice?
            as_read, _ = rig.run(result["invoice"], result["invoice"]["supplier_name"], text, boxes, result.get("evidence"), ocr)
            supplier["flagged"] = any("supplier" in (i.get("message") or "").lower() for i in new_issues(as_read, baseline))
            next(w for w in scored["wrong"] if w["field"] == "supplier_name")["flagged"] = supplier["flagged"]
        entry.update(scored)
        entry["rules_error"] = rules_error
        entry["status"] = (view or {}).get("status")
        if inv and view is not None:
            items, columns, wrong_items = score_items(truth, result["invoice"].get("lines") or [], scored["pairs"], scored["phantom"], view, rows)
            for j in scored["dropped"]:
                cls = truth["lines"][j]["item_class"]
                items.setdefault(cls, dict(ITEM_CELL))
                items[cls]["row not read"] += 1
            entry.update(items=items, columns=columns, wrong_items=wrong_items, issues=len(view.get("issues") or []))
        results.append(entry)
    (out / "results.json").write_text(json.dumps(results, default=str))
    (out / "report.md").write_text(report(results, HEADER_FIELDS, LINE_FIELDS))
    print((out / "report.md").read_text()[:args.show])


def _table(rows, heads):
    lines = ["| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def report(results, header_fields, line_fields):
    groups = defaultdict(list)
    for r in results:
        groups[(r["family"], r["variant"])].append(r)
    parts = ["Synthetic results. They are not a measure of the real invoices.", ""]
    rows = []
    for (family, variant), docs in sorted(groups.items()):
        fine = [d for d in docs if "fields" in d]
        cells = []
        total = {"correct": 0, "blank": 0, "WRONG": 0}
        silent = 0
        for d in fine:
            for v in d["fields"].values():
                total[v["verdict"]] += 1
                silent += v["verdict"] == "WRONG" and not v["flagged"]
        cells = [family, variant, len(docs), sum("crash" in d for d in docs), total["correct"], total["blank"], total["WRONG"], silent,
                 sum(d["line_verdict"] == "correct" for d in fine), sum(d["line_verdict"] == "blank" for d in fine), sum(d["line_verdict"] == "WRONG" for d in fine),
                 sum(d["dropped_passes_checks"] for d in fine), sum(bool(d["ai_need"]) for d in fine)]
        rows.append(cells)
    parts += ["## Header fields per family (7 fields a document)", "", _table(rows, ["family", "variant", "docs", "crash", "correct", "blank", "WRONG", "WRONG no flag", "lines ok", "no lines", "lines wrong", "drop passes checks", "would ask AI"]), ""]
    for variant in sorted({v for _, v in groups}):
        rows = []
        for field in header_fields:
            count = {"correct": 0, "blank": 0, "WRONG": 0, "silent": 0}
            for (_, v), docs in groups.items():
                if v != variant:
                    continue
                for d in docs:
                    cell = d.get("fields", {}).get(field)
                    if cell:
                        count[cell["verdict"]] += 1
                        count["silent"] += cell["verdict"] == "WRONG" and not cell["flagged"]
            rows.append([field, *count.values()])
        for field in line_fields:
            count = {"correct": 0, "blank": 0, "WRONG": 0, "silent": 0}
            for (_, v), docs in groups.items():
                if v != variant:
                    continue
                for d in docs:
                    for k, n in d.get("lines", {}).get(field, {}).items():
                        count[k] += n
            rows.append(["line." + field, *count.values()])
        parts += [f"## Per field, {variant}", "", _table(rows, ["field", "correct", "blank", "WRONG", "WRONG no flag"]), ""]
        items = defaultdict(lambda: dict(ITEM_CELL))
        columns = defaultdict(lambda: {"correct": 0, "blank": 0, "WRONG": 0})
        for (_, v), docs in groups.items():
            if v != variant:
                continue
            for d in docs:
                for cls, cell in (d.get("items") or {}).items():
                    for k, n in cell.items():
                        items[cls][k] += n
                for name, cell in (d.get("columns") or {}).items():
                    for k, n in cell.items():
                        columns[name][k] += n
        if items:
            parts += [f"## Final Item ID in the exported Details sheet, {variant}", "", _table([[k, *v.values()] for k, v in sorted(items.items())], ["class", "correct", "blank", "WRONG", "WRONG no flag", "row not read"]), ""]
            parts += [f"## Related export columns, {variant}", "", _table([[k, *v.values()] for k, v in sorted(columns.items())], ["column", "correct", "blank", "WRONG"]), ""]
    return "\n".join(parts)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("build", "read", "score", "files"))
    p.add_argument("--out", required=True)
    from .generator import SEED
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--only", default="")
    p.add_argument("--variant", default="")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--show", type=int, default=20000)
    args = p.parse_args()
    from .files import files
    {"build": build, "read": read, "score": score, "files": files}[args.command](args)


if __name__ == "__main__":
    main()
