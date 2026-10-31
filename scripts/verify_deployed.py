"""Prove each local reader reads on the deployed app: the engine scorecard, with the deployed HTTP API as its target.

    INV_STUDIO_TOKEN=... .venv/bin/python scripts/verify_deployed.py --url https://APP CORPUS
    .venv/bin/python scripts/verify_deployed.py --url URL CORPUS --local DIR/scorecard.json --out DIR
                     [--token-file FILE] [--engines invoice2data,docling] [--no-chain] [--job-timeout SECONDS]
                     [--poll SECONDS] [--keep-jobs] [--redact-names] [--no-timings]

CORPUS is the scorecard's corpus (scripts/engine_scorecard.py, either layout). The scorecard's run_corpus walks it,
validates every expected JSON, scores and summarises; this script only supplies the target that reads (Deployed). It
uploads each case the way the browser does (POST /api/preflight, /api/preflight/confirm, /api/invoices, then GET
/api/jobs/{id} until the job leaves queued/processing), once per reader alone and once through the auto chain, with
the options the local target uses:

- a reader alone: engine=E, prefer_native_text=False, ai_fallback=False
- the auto chain: engine="auto", prefer_native_text=True, ai_fallback=False

A finished job carries process()'s own result (selected_engine, trace, readers, invoice), so it becomes a run through
the scorecard's run_from_result, exactly as a local read does. Nothing here extracts or compares anything of its own.

Run status, besides the scorecard's:
  crashed      the app refused a request (path and HTTP status recorded) or the job ended in error (the job status,
               the exception class the app names, and its extraction_status); the app's message is not copied
  timed_out    the job was still queued or processing after --job-timeout seconds (detail "job still <status>"); in
               the chain summary its AI need is "not_finished"
  failed       detail "ai_called": the job's trace holds an AI call or readers.ai.status says the AI ran. With
               ai_fallback off that must never happen, and a read the AI helped is not the reader's, so it is never
               scored

Before any upload /health must answer and /api/session must accept the token. The readers the scorecard reports
("engines") are the deployed container's, from /api/state, so the table shows Azure's readers, not this machine's.
The app's cloud flag, sign-in role, legacy demo references and learned-template summary are recorded: references and
learned templates can make a deployed read differ from a fresh local one, and the report says so.

Verdict, per reader and for the chain; the exit code is 0 only when every one is "proved":
  proved       the deployed container has the reader installed with its models, it read at least one case, the AI was
               never called, and with --local every case matches the local scorecard: same status, same outcome per
               field, same lines missing and extra, same exact; for the chain also the same winner and AI need
  not_proved   anything above failed; "why" lists each reason
Without --local accuracy is measured but not compared, and "proved" means only that the reader runs in the deployed
app. Pass --local (scorecard.json from engine_scorecard.py over the same corpus, same --redact-names choice) to prove
the deployed readers read as the local ones do, so a deploy cannot quietly hand everything to the AI.

Sign-in: the deployed app takes an Entra access token for its own API, the one the signed-in browser sends. Give it in
INV_STUDIO_TOKEN or --token-file. It is sent to --url only and never written anywhere. A local app with no cloud
sign-in needs none.

Clean-up: the jobs this run uploaded, and only those, by id, are deleted at the end; that needs the admin app role.
With a member token, or --keep-jobs, they stay in the workspace and the report counts them.

Data: the corpus documents go to --url and nowhere else. The output holds what the scorecard's does: statuses, scores,
field names and case names (--redact-names replaces those), never invoice content or the app's error text. Output:
the text on stdout, and with --out DIR also verify-deployed.txt and verify-deployed.json (sorted keys).

Exit codes: 0 every reader proved; 1 a reader not proved; 2 the app could not be reached or refused the token, or the
corpus has no cases.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_scorecard as scorecard  # noqa: E402

HEADERS = {"X-Studio-Request": "1"}  # the app's cross-site request guard, sent by the browser on every call
RUNNING = ("queued", "processing")
AI_IDLE = (None, "off", "skipped", "unavailable")  # readers.ai.status values that mean the AI did not run
# What has to agree between a deployed run and the local run of the same case.
SAME = ("status", "fields", "lines_missing", "lines_extra", "exact")
SAME_CHAIN = SAME + ("selected_engine", "need")


class Unreachable(RuntimeError):
    """The app cannot be checked at all: not reachable, or it refused the token."""


def options_for(run):
    """The local target's ProcessingOptions for one reader alone or the chain, as the upload form sends them."""
    return {"engine": run, "prefer_native_text": run == scorecard.CHAIN, "ai_fallback": False}


def ai_calls(job):
    """The AI calls in a job: with the AI off there must be none.

    process() writes one trace entry per AI call, read or failed, and only those carry a "role"; the provider's other
    entries (skipped, needs_connection) and local steps such as guard_printed are not calls. readers.ai.status is
    checked too, so a call is caught even if its trace entry were missing.
    """
    calls = [t for t in job.get("trace") or [] if "role" in t or t.get("method") == "vision_ai"]
    status = ((job.get("readers") or {}).get("ai") or {}).get("status")
    return calls or ([{"readers.ai.status": status}] if status not in AI_IDLE else [])


def job_run(job, run, seconds):
    """A job as the deployed app left it, as a scorecard run."""
    if job.get("status") in RUNNING:
        # The chain summary counts every timed-out run's winner and AI need: this one has neither yet.
        chain = {"selected_engine": None, "need": "not_finished", "unavailable_in_chain": []} if run == scorecard.CHAIN else {}
        return {"status": "timed_out", "detail": ["job still " + job["status"]], "seconds": seconds, **chain}
    if job.get("status") != "review":
        # The app's own message names the exception class in brackets; the rest of the message is not copied.
        found = re.search(r"\(([A-Za-z_][A-Za-z0-9_]*)\)", str(job.get("error") or ""))
        detail = [f"job {job.get('status')}"] + ([found.group(1)] if found else []) + (
            [job["extraction_status"]] if job.get("extraction_status") else [])
        return {"status": "crashed", "detail": detail, "seconds": seconds}
    result = scorecard.run_from_result(job, run, seconds)
    if ai_calls(job):
        # Kept with its winner and AI need for the chain summary; as it is not "read", it is never scored.
        result.update(status="failed", detail=["ai_called"])
    return result


class Deployed:
    """The deployed app as a scorecard target (see scorecard.LocalChain): its HTTP API, called as the browser calls it."""

    name = "deployed"
    runs = scorecard.ENGINES + (scorecard.CHAIN,)

    def __init__(self, client, poll=2.0, job_timeout=900.0, clock=time.monotonic, sleep=time.sleep):
        self.client, self.poll, self.job_timeout, self.clock, self.sleep = client, poll, job_timeout, clock, sleep
        self.created = []  # [job id, revision] of every job this run uploaded
        self.ai_called = {}  # run -> [document names] whose job called the AI

    def get(self, path):
        response = self.client.get(path, headers=HEADERS)
        response.raise_for_status()
        return response.json()

    def post(self, path, **kwargs):
        response = self.client.post(path, headers=HEADERS, **kwargs)
        response.raise_for_status()
        return response.json()

    def connect(self):
        """What the deployed app is, before anything is uploaded; Unreachable if it cannot be checked."""
        try:
            health = self.get("/health")
            cloud = bool(self.get("/api/public-config").get("cloud"))
            session = self.get("/api/session")
            state = self.get("/api/state")
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            raise Unreachable(f"{e.request.method} {e.request.url.path} answered HTTP {code}"
                              + (": the token was refused or carries no app role" if code in (401, 403) else ""))
        except httpx.HTTPError as e:
            raise Unreachable(f"the app could not be reached ({type(e).__name__})")
        if health.get("status") != "ok":
            raise Unreachable("/health did not answer ok")
        try:
            learned = self.get("/api/learned")
        except httpx.HTTPError:
            learned = {"enabled": None}
        return {"version": health.get("version"), "cloud": cloud, "role": session.get("role"),
                "legacy_references": bool(state.get("legacy_references")),
                "learned": {k: v for k, v in learned.items() if isinstance(v, (bool, int)) or v is None}}

    def fields(self):
        # The deployed app runs this code base, so its output has the fields of this checkout's models.
        sys.path.insert(0, str(scorecard.ROOT))
        from app import models
        return ([f for f in models.Invoice.model_fields if f != "lines"], list(models.Line.model_fields))

    def readers(self):
        return {c["id"]: {"installed": c["installed"], "models_blocked": c["models_blocked"], "version": c["version"]}
                for c in self.get("/api/state").get("engines") or []}

    def read(self, path, run):
        options = options_for(run)
        content = path.read_bytes()
        start = self.clock()
        try:
            plan = self.post("/api/preflight", json={"options": options,
                                                     "files": [{"name": path.name, "size": len(content)}]})
            if plan.get("blocking"):
                return {"status": "crashed", "detail": ["preflight blocking"], "seconds": self.clock() - start}
            self.post("/api/preflight/confirm", json={"token": plan["token"]})
            job = self.post("/api/invoices", data={"options": json.dumps(options), "preflight_token": plan["token"]},
                            files={"file": (path.name, content)})
        except httpx.HTTPStatusError as e:
            return {"status": "crashed", "detail": [f"{e.request.url.path} HTTP {e.response.status_code}"],
                    "seconds": self.clock() - start}
        except httpx.HTTPError as e:
            return {"status": "crashed", "detail": [type(e).__name__], "seconds": self.clock() - start}
        self.created.append([job["id"], job.get("revision", 1)])
        deadline = start + self.job_timeout
        while job.get("status") in RUNNING and self.clock() < deadline:
            self.sleep(self.poll)
            try:
                job = self.get(f"/api/jobs/{job['id']}")
            except httpx.HTTPError:
                continue  # a dropped poll is tried again until the deadline; the job itself is unaffected
        self.created[-1][1] = job.get("revision", 1)
        result = job_run(job, run, self.clock() - start)
        if result.get("detail") == ["ai_called"]:
            self.ai_called.setdefault(run, []).append(path)
        return result

    def clean_up(self):
        """Delete the jobs this run uploaded: (deleted, left, why left)."""
        deleted = 0
        for n in range(0, len(self.created), 100):
            batch = self.created[n:n + 100]
            try:
                self.post("/api/jobs/delete", json={"jobs": [{"id": i, "revision": r} for i, r in batch],
                                                    "confirm_permanent": True})
            except httpx.HTTPStatusError as e:
                return deleted, len(self.created) - deleted, ("deleting needs the admin app role"
                                                              if e.response.status_code == 403 else
                                                              f"/api/jobs/delete answered HTTP {e.response.status_code}")
            except httpx.HTTPError as e:
                return deleted, len(self.created) - deleted, type(e).__name__
            deleted += len(batch)
        return deleted, 0, None


def compare(deployed, local):
    """Per run: the cases whose deployed run differs from the local run, and in which keys."""
    differences = {}
    for run, cases in deployed["cases"].items():
        theirs = (local.get("cases") or {}).get(run)
        if theirs is None:
            differences[run] = {"*": ["not in the local scorecard"]}
            continue
        keys = SAME_CHAIN if run == scorecard.CHAIN else SAME
        found = {}
        for case in sorted(set(cases) | set(theirs)):
            mine, other = cases.get(case), theirs.get(case)
            if mine is None or other is None:
                found[case] = ["only " + ("local" if mine is None else "deployed")]
                continue
            differ = [k for k in keys if _plain(mine.get(k)) != _plain(other.get(k))]
            if differ:
                found[case] = differ
        if found:
            differences[run] = found
    return differences


def _plain(value):
    # A report read back from JSON has lists where the scorecard holds (field, outcome) tuples.
    return json.loads(json.dumps(value))


def verdicts(report, differences=None):
    """proved / not_proved per run, with every reason."""
    result = {}
    for run, summary in report["summary"].items():
        why = []
        if run != scorecard.CHAIN:
            reader = report["engines"].get(run)
            if not reader or not reader["installed"]:
                why.append("not installed in the deployed app")
            elif reader["models_blocked"]:
                why.append("installed, but its models are blocked in the deployed app")
        if summary["read"] == 0:
            why.append("read no case")
        if report["ai_called"].get(run):
            why.append(f"the AI was called on {len(report['ai_called'][run])} case(s)")
        if differences is not None and differences.get(run):
            why.append(f"differs from local on {len(differences[run])} case(s)")
        result[run] = {"verdict": "not_proved" if why else "proved", "why": why,
                       "compared_with_local": differences is not None}
    return result


def text(report):
    app = report["app"]
    out = [f"Deployed app {app['version']}; cloud sign-in {'on' if app['cloud'] else 'off'}; signed in as {app['role']}"]
    if app["legacy_references"]:
        out.append("NOTE: the app enriches reads from legacy demo references; a deployed field can differ from local.")
    if app["learned"].get("enabled"):
        out.append("NOTE: learned templates are on in the app; a learned template can change a deployed read.")
    out = ["\n".join(out), scorecard.table(report).rstrip("\n")]
    if report["ai_called"]:
        out.append("AI CALLED with the AI off: " + "; ".join(f"{r}: {', '.join(c)}" for r, c in report["ai_called"].items()))
    if "differences" in report:
        lines = ["Deployed vs local: " + ("every case matches" if not report["differences"] else "differences")]
        lines += [f"  {run} {case}: {', '.join(keys)}" for run, cases in report["differences"].items()
                  for case, keys in cases.items()]
        out.append("\n".join(lines))
    lines = ["Verdict:"]
    for run, v in report["verdicts"].items():
        note = f" ({'; '.join(v['why'])})" if v["why"] else "" if v["compared_with_local"] else " (runs; not compared with local)"
        lines.append(f"  {run:<14}{v['verdict']}{note}")
    out.append("\n".join(lines))
    c = report["clean_up"]
    out.append(f"Jobs uploaded {c['uploaded']}, deleted {c['deleted']}" + (f", left {c['left']} ({c['why']})" if c["left"] else ""))
    return "\n\n".join(out) + "\n"


def main(argv=None, client=None):
    """`client` is the test seam: an httpx.Client already pointed at an app (a TestClient in the tests)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--url", required=client is None, help="the deployed app, e.g. https://inv-studio.example")
    parser.add_argument("--token-file", type=Path, help="a file holding the access token (else INV_STUDIO_TOKEN)")
    parser.add_argument("--local", type=Path, help="scorecard.json from engine_scorecard.py to compare with")
    parser.add_argument("--out", type=Path, help="write verify-deployed.txt and verify-deployed.json here")
    parser.add_argument("--engines", default=",".join(scorecard.ENGINES), help="comma-separated readers to run alone")
    parser.add_argument("--no-chain", action="store_true", help="do not run the auto chain")
    parser.add_argument("--job-timeout", type=float, default=900, help="seconds to wait for one job (default 900)")
    parser.add_argument("--poll", type=float, default=2, help="seconds between job polls (default 2)")
    parser.add_argument("--keep-jobs", action="store_true", help="leave the uploaded jobs in the workspace")
    parser.add_argument("--redact-names", action="store_true", help="name cases case-001... instead of their names")
    parser.add_argument("--no-timings", action="store_true", help="drop seconds so runs diff cleanly")
    args = parser.parse_args(argv)
    names = [e.strip() for e in args.engines.split(",") if e.strip()]
    unknown = [e for e in names if e not in scorecard.ENGINES]
    if unknown:
        parser.error(f"unknown reader {', '.join(unknown)}; choose from {', '.join(scorecard.ENGINES)}")
    if not args.no_chain:
        names.append(scorecard.CHAIN)
    if not names:
        parser.error("nothing to run")
    if not args.corpus.is_dir():
        parser.error(f"{args.corpus} is not a directory")
    if not scorecard.find_cases(args.corpus):
        print(f"No cases in {args.corpus}: no document, and no case directory.", file=sys.stderr)
        return 2
    local = json.loads(args.local.read_text(encoding="utf-8")) if args.local else None
    if client is None:
        token = (args.token_file.read_text(encoding="utf-8") if args.token_file else os.getenv("INV_STUDIO_TOKEN", "")).strip()
        client = httpx.Client(base_url=args.url.rstrip("/"), timeout=60,
                              headers={"Authorization": "Bearer " + token} if token else {})
    target = Deployed(client, poll=args.poll, job_timeout=args.job_timeout)
    try:
        app = target.connect()
        report = scorecard.run_corpus(args.corpus, names, target, redact=args.redact_names)
    except Unreachable as e:
        print(f"Cannot check the deployed app: {e}", file=sys.stderr)
        return 2
    finally:
        deleted, left, why = (0, len(target.created), "--keep-jobs") if args.keep_jobs else target.clean_up()
    # Case names as run_corpus labels them, so a redacted run names no file here either.
    label = {case["document"]: (f"case-{n:03d}" if args.redact_names else case["name"])
             for n, case in enumerate(scorecard.find_cases(args.corpus), 1)}
    report["app"] = app
    report["ai_called"] = {run: sorted(label[p] for p in paths) for run, paths in sorted(target.ai_called.items())}
    report["clean_up"] = {"uploaded": len(target.created), "deleted": deleted, "left": left, "why": why}
    if local is not None:
        report["differences"] = compare(report, local)
    report["verdicts"] = verdicts(report, report.get("differences"))
    if args.no_timings:
        report = scorecard.strip_timings(report)
    out = text(report)
    sys.stdout.write(out)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "verify-deployed.txt").write_text(out, encoding="utf-8")
        (args.out / "verify-deployed.json").write_text(json.dumps(report, indent=1, sort_keys=True, default=str) + "\n",
                                                       encoding="utf-8")
    return 0 if all(v["verdict"] == "proved" for v in report["verdicts"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
