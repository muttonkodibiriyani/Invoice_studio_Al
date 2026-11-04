"""Run real invoices through the deployed app end to end, as a user would, and record what happened to each.

    INV_STUDIO_TOKEN=... python3 scripts/live_invoice_run.py --url https://APP DIR [--out OUT] [--token-file FILE]
                         [--job-timeout SECONDS] [--poll SECONDS] [--clean-up] [--redact-names]

DIR holds the invoices (any document scripts/engine_scorecard.py reads), flat or one per case directory. Each goes
through the browser's path (preflight, confirm, upload, poll; verify_deployed.Deployed does the calls) with the
upload form's defaults: engine "auto", native text preferred, and the AI on as the fallback, as production runs.

Unlike verify_deployed.py nothing here fails a read for calling the AI: the AI calls are counted, per invoice and in
total, because that is what the live run has to report. Per invoice the record holds: the job's final status (ready,
review, error, or timed_out), the reader that won, the AI calls made and the AI need, header fields filled out of the
model's, lines read, and the validation issue codes holding it. When an expected JSON sits beside the document (the
scorecard's layout) the read is also scored field by field with the scorecard's rules.

Export is not run: it needs a reviewer to confirm each invoice, which is the human half of the live run.

Data: documents go to --url only. The output holds statuses, counts, field names, issue codes and file names
(--redact-names replaces those), never invoice values or the app's error text. Output: the text on stdout, and with
--out DIR also live-run.txt and live-run.json. Jobs this run uploaded stay in the workspace for review unless
--clean-up is given (admin role).

Exit codes: 0 every invoice reached ready or review; 1 one or more errored or timed out; 2 the app could not be
reached or refused the token, or DIR has no document.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_scorecard as scorecard  # noqa: E402
import verify_deployed as deployed  # noqa: E402

OPTIONS = {"engine": "auto", "prefer_native_text": True, "ai_fallback": True}  # the upload form's defaults
DONE = ("ready", "review")


def documents(root):
    """(document, expected JSON path or None), in sorted order, from either corpus layout."""
    found = []
    for path in sorted(root.iterdir()):
        if path.is_dir():
            docs = [p for p in sorted(path.iterdir()) if scorecard.is_document(p)]
            found += [(d, path / scorecard.EXPECTED_NAME) for d in docs]
        elif scorecard.is_document(path):
            found.append((path, path.with_suffix(".json")))
    return [(d, e if e.is_file() else None) for d, e in found]


class Live(deployed.Deployed):
    """The deployed app with production options; keeps each finished job instead of judging it."""

    def read(self, path, run=None):
        content = path.read_bytes()
        start = self.clock()
        try:
            plan = self.post("/api/preflight", json={"options": OPTIONS, "files": [{"name": path.name, "size": len(content)}]})
            if plan.get("blocking"):
                return {"status": "error", "detail": "preflight blocking"}, 0.0
            self.post("/api/preflight/confirm", json={"token": plan["token"]})
            job = self.post("/api/invoices", data={"options": json.dumps(OPTIONS), "preflight_token": plan["token"]},
                            files={"file": (path.name, content)})
        except httpx.HTTPStatusError as e:
            return {"status": "error", "detail": f"{e.request.url.path} HTTP {e.response.status_code}"}, 0.0
        except httpx.HTTPError as e:
            return {"status": "error", "detail": type(e).__name__}, 0.0
        self.created.append([job["id"], job.get("revision", 1)])
        while job.get("status") in deployed.RUNNING and self.clock() < start + self.job_timeout:
            self.sleep(self.poll)
            try:
                job = self.get(f"/api/jobs/{job['id']}")
            except httpx.HTTPError:
                continue
        self.created[-1][1] = job.get("revision", 1)
        return job, self.clock() - start


def record(job, seconds, expected, header_fields):
    """What one invoice's job says, without any invoice value."""
    status = job.get("status")
    if status in deployed.RUNNING:
        return {"status": "timed_out", "detail": f"job still {status}", "seconds": seconds}
    if status not in DONE:
        return {"status": "error", "detail": " ".join(filter(None, (f"job {status}", job.get("extraction_status")))),
                "seconds": seconds}
    invoice = job.get("invoice") or {}
    ai = (job.get("readers") or {}).get("ai") or {}
    out = {"status": status, "seconds": seconds, "selected_engine": job.get("selected_engine"),
           "ai_calls": len(deployed.ai_calls(job)), "ai_status": ai.get("status"), "ai_need": ai.get("need"),
           "header_filled": sum(1 for f in header_fields if invoice.get(f) not in (None, "")),
           "header_fields": len(header_fields), "lines": len(invoice.get("lines") or []),
           "issues": sorted({i.get("code") for i in (job.get("validation") or {}).get("issues") or []})}
    if expected is not None:
        try:
            scored = scorecard.score_case(json.loads(expected.read_text(encoding="utf-8")), invoice)
            out["score"] = {"correct": sum(1 for _, o in scored["fields"] if o == "correct"),
                            "scored": len(scored["fields"]), "exact": scored["exact"],
                            "wrong": [f for f, o in scored["fields"] if o != "correct"]}
        except scorecard.BadExpected as e:
            out["score"] = {"bad_expected": str(e)}
    return out


def text(report):
    app = report["app"]
    out = [f"Deployed app {app['version']}; cloud sign-in {'on' if app['cloud'] else 'off'}; signed in as {app['role']}",
           f"{'invoice':<40}{'status':<10}{'reader':<14}{'AI':>3}  {'header':>7}{'lines':>6}  issues / score"]
    for name, r in report["invoices"].items():
        if r["status"] not in DONE:
            out.append(f"{name:<40}{r['status']:<10}{r['detail']}")
            continue
        score = r.get("score") or {}
        extra = ",".join(r["issues"]) + (f"  score {score['correct']}/{score['scored']}" if "scored" in score else "")
        out.append(f"{name:<40}{r['status']:<10}{str(r['selected_engine']):<14}{r['ai_calls']:>3}  "
                   f"{r['header_filled']:>3}/{r['header_fields']:<3}{r['lines']:>6}  {extra}")
    t = report["totals"]
    out.append(f"\n{t['invoices']} invoices: {t['ready']} ready, {t['review']} review, {t['error']} error, "
               f"{t['timed_out']} timed out; AI calls {t['ai_calls']} on {t['ai_invoices']} invoice(s)")
    c = report["clean_up"]
    out.append(f"Jobs uploaded {c['uploaded']}, deleted {c['deleted']}" + (f", left {c['left']} ({c['why']})" if c["left"] else ""))
    return "\n".join(out) + "\n"


def main(argv=None, client=None):
    """`client` is the test seam, as in verify_deployed.main."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("invoices", type=Path)
    parser.add_argument("--url", required=client is None, help="the deployed app, e.g. https://inv-studio.example")
    parser.add_argument("--token-file", type=Path, help="a file holding the access token (else INV_STUDIO_TOKEN)")
    parser.add_argument("--out", type=Path, help="write live-run.txt and live-run.json here")
    parser.add_argument("--job-timeout", type=float, default=900, help="seconds to wait for one job (default 900)")
    parser.add_argument("--poll", type=float, default=2, help="seconds between job polls (default 2)")
    parser.add_argument("--clean-up", action="store_true", help="delete the uploaded jobs at the end (admin role)")
    parser.add_argument("--redact-names", action="store_true", help="name invoices invoice-001... instead")
    args = parser.parse_args(argv)
    if not args.invoices.is_dir():
        parser.error(f"{args.invoices} is not a directory")
    docs = documents(args.invoices)
    if not docs:
        print(f"No document in {args.invoices}.", file=sys.stderr)
        return 2
    if client is None:
        token = (args.token_file.read_text(encoding="utf-8") if args.token_file else os.getenv("INV_STUDIO_TOKEN", "")).strip()
        client = httpx.Client(base_url=args.url.rstrip("/"), timeout=60,
                              headers={"Authorization": "Bearer " + token} if token else {})
    target = Live(client, poll=args.poll, job_timeout=args.job_timeout)
    try:
        app = target.connect()
    except deployed.Unreachable as e:
        print(f"Cannot reach the deployed app: {e}", file=sys.stderr)
        return 2
    header_fields = target.fields()[0]
    invoices = {}
    for n, (doc, expected) in enumerate(docs, 1):
        job, seconds = target.read(doc)
        name = f"invoice-{n:03d}" if args.redact_names else str(doc.relative_to(args.invoices))
        invoices[name] = record(job, round(seconds, 1), expected, header_fields)
        print(f"{name}: {invoices[name]['status']}", file=sys.stderr)
    deleted, left, why = target.clean_up() if args.clean_up else (0, len(target.created), "kept for review")
    statuses = [r["status"] for r in invoices.values()]
    report = {"app": app, "options": OPTIONS, "invoices": invoices,
              "totals": {"invoices": len(invoices), **{s: statuses.count(s) for s in ("ready", "review", "error", "timed_out")},
                         "ai_calls": sum(r.get("ai_calls", 0) for r in invoices.values()),
                         "ai_invoices": sum(1 for r in invoices.values() if r.get("ai_calls"))},
              "clean_up": {"uploaded": len(target.created), "deleted": deleted, "left": left, "why": why}}
    out = text(report)
    sys.stdout.write(out)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "live-run.txt").write_text(out, encoding="utf-8")
        (args.out / "live-run.json").write_text(json.dumps(report, indent=1, sort_keys=True, default=str) + "\n",
                                                encoding="utf-8")
    return 0 if all(s in DONE for s in statuses) else 1


if __name__ == "__main__":
    sys.exit(main())
