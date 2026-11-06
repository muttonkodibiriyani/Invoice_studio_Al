"""What the AI fallback does to real invoices: each read through the deployed app with AI off, then on, scored per cell.

    INV_STUDIO_TOKEN=... python3 scripts/ai_fallback_compare.py --url https://APP DIR [--out OUT] [--token-file FILE]
                         [--job-timeout SECONDS] [--poll SECONDS] [--clean-up] [--redact-names]

DIR is laid out as scripts/live_invoice_run.py expects, with the truth files beside the documents: an invoice with no
truth file is refused, since nothing could be counted for it. For the first revision with AI on (docs/AZURE.md §7).

Per invoice and in total: correct, blank and wrong cells with AI off and with AI on; the cells the AI filled and those
it flagged; suggestions. Per AI-added rule: the lines it filled, and how many of those are right or wrong against the
truth file's item_id, so wrong picks can be measured. The AI-off pass goes first: it reads with no AI and no added rule.
Rules the AI-on pass adds stay in the app, like any others; remove them on Engines & AI or with DELETE /api/ai-rules/{id}.

Data: documents go to --url only, and the app sends them on only to the Azure AI Foundry it is connected to. The output
holds counts, field names, rule ids and file names (--redact-names replaces those), never an invoice value.

Exit codes: 0 every invoice was read both ways; 1 one or more errored or timed out; 2 the app could not be reached or
refused the token, Azure AI Foundry is not connected, or DIR has no document with a truth file.
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_scorecard as scorecard  # noqa: E402
import live_invoice_run as live  # noqa: E402
import verify_deployed as deployed  # noqa: E402

BLANK = {"missing"}  # the app left it empty; "unexpected" and "wrong" both wrote a value that should not be there


def cells(job, truth):
    """{correct, blank, wrong} of one read against its truth file, or None when the read did not finish."""
    if job.get("status") not in live.DONE:
        return None
    count = Counter("correct" if outcome == "correct" else "blank" if outcome in BLANK else "wrong"
                    for _, outcome in scorecard.score_case(truth, job.get("invoice") or {})["fields"])
    return {kind: count[kind] for kind in ("correct", "blank", "wrong")}


def rule_results(job, truth):
    """{rule id: {filled, right, wrong, unscored}} for the lines the AI-added rules filled on one read."""
    results = {}
    lines = (job.get("invoice") or {}).get("lines") or []
    want = truth.get("lines") or []
    key = scorecard.line_key_field(want) if want else None
    for fill in ((job.get("readers") or {}).get("ai") or {}).get("rules_applied") or []:
        result = results.setdefault(fill["rule"], {"filled": 0, "right": 0, "wrong": 0, "unscored": 0})
        result["filled"] += 1
        read = lines[fill["line"]] if fill["line"] < len(lines) else {}
        match = next((x for x in want if key and scorecard.norm_string(x.get(key)) == scorecard.norm_string(read.get(key))), None)
        if match is None or "item_id" not in match:
            result["unscored"] += 1
        else:
            result["right" if scorecard.compare_field("item_id", match["item_id"], read.get("item_id")) == "correct" else "wrong"] += 1
    return results


def text(report):
    out = [f"{'invoice':<40}{'AI off: ok/blank/wrong':>24}{'AI on: ok/blank/wrong':>24}{'filled':>8}{'flagged':>9}{'hints':>7}"]
    show = lambda c: "not read" if c is None else f"{c['correct']}/{c['blank']}/{c['wrong']}"  # noqa: E731
    for name, r in report["invoices"].items():
        out.append(f"{name:<40}{show(r['off']):>24}{show(r['on']):>24}{r['filled']:>8}{r['flagged']:>9}{r['suggestions']:>7}")
    t = report["totals"]
    out.append(f"\nAll {t['invoices']} invoices, both reads finished on {t['both_read']}: AI off {show(t['off'])}, AI on {show(t['on'])}; "
               f"{t['filled']} cells filled by the AI, {t['flagged']} flagged, {t['suggestions']} suggestions.")
    for rule, r in report["rules"].items():
        out.append(f"Rule {rule}: filled {r['filled']} lines here ({r['right']} right, {r['wrong']} wrong, "
                   f"{r['unscored']} with no truth to score), {r['lines_filled']} in all")
    if not report["rules"]:
        out.append("No AI-added rule filled a line.")
    return "\n".join(out) + "\n"


def main(argv=None, client=None):
    """`client` is the test seam, as in verify_deployed.main."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("invoices", type=Path)
    parser.add_argument("--url", required=client is None, help="the deployed app, e.g. https://inv-studio.example")
    parser.add_argument("--token-file", type=Path, help="a file holding the access token (else INV_STUDIO_TOKEN)")
    parser.add_argument("--out", type=Path, help="write ai-compare.txt and ai-compare.json here")
    parser.add_argument("--job-timeout", type=float, default=900, help="seconds to wait for one job (default 900)")
    parser.add_argument("--poll", type=float, default=2, help="seconds between job polls (default 2)")
    parser.add_argument("--clean-up", action="store_true", help="delete the uploaded jobs at the end (admin role)")
    parser.add_argument("--redact-names", action="store_true", help="name invoices invoice-001... instead")
    args = parser.parse_args(argv)
    if not args.invoices.is_dir():
        parser.error(f"{args.invoices} is not a directory")
    docs = [(doc, truth) for doc, truth in live.documents(args.invoices) if truth]
    if not docs:
        print(f"No document with a truth file in {args.invoices}.", file=sys.stderr)
        return 2
    if client is None:
        token = (args.token_file.read_text(encoding="utf-8") if args.token_file else os.getenv("INV_STUDIO_TOKEN", "")).strip()
        client = httpx.Client(base_url=args.url.rstrip("/"), timeout=60,
                              headers={"Authorization": "Bearer " + token} if token else {})
    target = live.Live(client, poll=args.poll, job_timeout=args.job_timeout)
    try:
        target.connect()
        state = target.get("/api/state")
    except (deployed.Unreachable, httpx.HTTPError) as e:
        print(f"Cannot reach the deployed app: {type(e).__name__}", file=sys.stderr)
        return 2
    if not (state.get("connections") or {}).get("azure") or not state.get("azure_deployment"):
        print("Azure AI Foundry is not connected on this app, so an AI-on read would prove nothing. Nothing was uploaded.",
              file=sys.stderr)
        return 2
    passes = {"off": {**live.OPTIONS, "ai_fallback": False},
              "on": {**live.OPTIONS, "provider": "azure", "model": state["azure_deployment"]}}
    invoices, used = {}, {}
    for n, (doc, truth_path) in enumerate(docs, 1):
        name = f"invoice-{n:03d}" if args.redact_names else str(doc.relative_to(args.invoices))
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        jobs = {}
        for which, options in passes.items():
            target.options = options
            jobs[which] = target.read(doc)[0]
        ai = (jobs["on"].get("readers") or {}).get("ai") or {}
        invoices[name] = {"off": cells(jobs["off"], truth), "on": cells(jobs["on"], truth), "ai_status": ai.get("status"),
                          "ai_calls": ai.get("calls", 0), "filled": len(ai.get("filled") or []), "flagged": len(ai.get("flagged") or []),
                          "suggestions": len(ai.get("suggestions") or []), "rules_added": len(ai.get("rules_added") or [])}
        for rule, result in rule_results(jobs["on"], truth).items():
            total = used.setdefault(rule, Counter())
            total.update(result)
        print(f"{name}: read both ways", file=sys.stderr)
    try:
        held = {r["id"]: r.get("lines_filled", 0) for r in target.get("/api/ai-rules")["rules"]}
    except httpx.HTTPError:
        held = {}
    finished = [r for r in invoices.values() if r["off"] and r["on"]]
    total = lambda which: {k: sum(r[which][k] for r in finished) for k in ("correct", "blank", "wrong")}  # noqa: E731
    report = {"invoices": invoices,
              "rules": {rule: {**{k: count[k] for k in ("filled", "right", "wrong", "unscored")}, "lines_filled": held.get(rule, 0)}
                        for rule, count in used.items()},
              "totals": {"invoices": len(invoices), "both_read": len(finished), "off": total("off"), "on": total("on"),
                         **{k: sum(r[k] for r in invoices.values()) for k in ("filled", "flagged", "suggestions", "rules_added")}}}
    if args.clean_up:
        target.clean_up()
    out = text(report)
    sys.stdout.write(out)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "ai-compare.txt").write_text(out, encoding="utf-8")
        (args.out / "ai-compare.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if len(finished) == len(invoices) else 1


if __name__ == "__main__":
    sys.exit(main())
