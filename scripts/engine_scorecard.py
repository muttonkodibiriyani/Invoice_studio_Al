"""Score every local reader, and the auto chain with the AI off, field by field over a corpus.

    .venv/bin/python scripts/engine_scorecard.py samples
    .venv/bin/python scripts/engine_scorecard.py CORPUS [--target local] [--out DIR] [--engines invoice2data,docling]
                                                 [--no-chain] [--timeout SECONDS] [--redact-names] [--no-timings]

CORPUS is a directory of cases, in either layout (both may be mixed; cases run in sorted name order):
- per case: CORPUS/<case>/ holding exactly one document (.pdf .png .jpg .jpeg .tif .tiff .webp .bmp) and
  expected.json; the case is named <case>/. Other files there are ignored.
- flat (samples/): a document with <stem>.json beside it; the case is named by the document's file name. A JSON with
  no document (samples/reference.json) is not a case.
A case directory with no document or two, or a document with no expected JSON, is reported as bad_expected with the
layout problem; it is never skipped silently. The expected JSON is the samples/*.json shape.

--target picks what reads the documents (TARGETS; a target is described on LocalChain). The only one today is
`local`: the app's own entry point app.engines.process() in this process, so the app reads exactly as it does for an
upload; nothing here extracts anything:

- each local reader alone: ProcessingOptions(engine=E, prefer_native_text=False, ai_fallback=False). Native-text
  preference is off so a PDF is read by the named reader, not by the embedded-text preflight. invoice2data on an image
  is the app's invoice2data + PaddleOCR path, so it needs PaddleOCR.
- the auto chain: ProcessingOptions(engine="auto", ai_fallback=False). The winner is process()'s selected_engine. The
  AI handover is process()'s own decision, result["readers"]["ai"]["need"]: "fallback" (the engines could not read)
  and "gap_fill" (they left gaps) hand over; "cross_check" (complete) and None (a purchase order) do not.

The AI is never called: the AI reader passed in raises, and a run that reaches it is reported as crashed. Each run gets
a fresh empty data root, so no learned template from earlier work changes a read. --timeout sets INV_ENGINE_TIMEOUT.

Run status, decided from process()'s result before any scoring:
  read         the reader returned an invoice (selected_engine is not "none"); only these runs are scored
  no_invoice   the reader ran and returned text or nothing, but no invoice
  unavailable  a reader the run needed is not installed, or its model download is refused
  timed_out    a reader ran past its time limit
  failed       a reader process failed
  crashed      process() itself raised (the exception class is recorded)
  bad_expected the expected JSON breaks the rules below; the case is not scored for anyone
Accuracy is computed over `read` runs only. Every other status is counted on its own line, never as a zero score and
never as a pass. "Read" counts read runs out of the cases attempted.

Comparison rules (the expected JSON defines what is scored: every key it has, nothing else):
- Every expected key must be a field of the target's own output (for `local`, app.models.Invoice and Line); one that
  is not, a typo such as invoice_number, makes the case bad_expected instead of a field that is always missing.
- A key missing from the read counts as null. null and "" are different values: one never matches the other.
- Outcome per field: correct; missing (expected a value, read null); unexpected (expected null, read a value); wrong
  (both have values that differ, or an amount or date that cannot be parsed). Only `correct` counts as correct.
- Amounts (net, tax, qty, price, net_amount, tax_amount): parsed as Decimal and equal within AMOUNT_TOLERANCE = 0.0005,
  half the smallest minor unit in use (KWD fils), so "800" equals "800.00" and one cent or one fils off is wrong.
  Thousands separators are not removed: the app returns plain numbers, and a read with "1,000" is wrong.
- Dates (date): normalised to a calendar date. ISO 8601 (2026-10-04, 20261004, 2026-10-04T00:00:00), year-first
  2026/10/04, and dates with a month name (04 Oct 2026, 4 October 2026, Oct 4, 2026, 04-Oct-2026). A numeric date with
  the year last (04/10/2026) is ambiguous and is never guessed: it is compared as a string and so is wrong.
- Every other field is a string: trimmed, internal whitespace runs collapsed to one space, case-folded. Nothing else
  (no punctuation stripping, no leading-zero stripping: a GTIN keeps its zeros).
- Line items are compared as a multiset keyed by one field, the first of sku, gtin, description that every expected
  line of the case fills; read lines are keyed by the same field, normalised as a string. Equal keys pair in order.
  A matched line scores each field its expected line has as lines[].<field>. An expected line with no match scores
  each of its fields `missing`. lines[] (the line set) is correct only when no expected line is missing and no extra
  line was read. If no field is filled on every expected line the case is bad_expected.
- A case is exact when every scored field is correct.

The readers installed when the run starts are recorded, and a change by its end is flagged as a warning.

Output: the table on stdout, and with --out DIR the same table as scorecard.txt and the result as scorecard.json
(sorted keys, stable order; --no-timings drops seconds so two runs can be diffed). Neither holds invoice content:
only statuses, scores, field names and case file names (--redact-names replaces those with case-001...). A reader's
failure message is not copied either, as it can quote the document; the status and exception class are.

Public API, for scripts that score another target (scripts/verify_deployed.py). Importing this module has no side
effects: no argparse, no file reads, no app import (LocalChain imports the app when constructed).
  score_case(expected: dict, actual: dict, fields=None) -> {"fields": [(name, outcome)], "lines_missing": int,
      "lines_extra": int, "line_key": str | None, "exact": bool}; raises BadExpected. fields = (header names,
      line names) of the target's output, or None to skip the name check.
  compare_field(field: str, expected, actual) -> "correct" | "missing" | "unexpected" | "wrong"
  check_field_names(expected: dict, fields) -> None; raises BadExpected
  check_expected(field: str, value) -> None; raises BadExpected for an amount or date that does not parse
  run_status(result: dict) -> (status: str, readers: list[str]) from a process()-shaped result
  run_from_result(result: dict, run: str, seconds: float) -> run dict (status, invoice, selected_engine, need ...)
  score_run(run: dict, expected: dict, fields=None) -> the run scored, invoice dropped; unread runs left unscored
  summarise(run_name: str, runs: {case: scored run}) -> per-run summary (statuses, read, accuracy, fields, loses)
  find_cases(corpus: Path) -> [{"name", "document", "expected", "layout_error"}]
  run_corpus(corpus: Path, names: list[str], target, redact=False) -> report dict; table(report) -> str
  strip_timings(report) -> the report without any "seconds", for diffing
  Constants: ENGINES, CHAIN, TARGETS, AMOUNT_FIELDS, DATE_FIELDS, AMOUNT_TOLERANCE, LINE_KEYS, HANDOVER_NEEDS, STATUSES
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
import types
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ENGINES = ("invoice2data", "paddleocr", "docling")
CHAIN = "auto"
DOCUMENT_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp")
AMOUNT_FIELDS = frozenset({"net", "tax", "qty", "price", "net_amount", "tax_amount"})
DATE_FIELDS = frozenset({"date"})
AMOUNT_TOLERANCE = Decimal("0.0005")
LINE_KEYS = ("sku", "gtin", "description")
LINE_SET = "lines[]"
EXPECTED_NAME = "expected.json"
HANDOVER_NEEDS = frozenset({"fallback", "gap_fill"})
STATUSES = ("read", "no_invoice", "unavailable", "timed_out", "failed", "crashed", "bad_expected")
DATE_FORMATS = ("%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
                "%d-%b-%Y", "%d-%B-%Y")


class BadExpected(ValueError):
    """The expected JSON cannot be scored against: the corpus, not a reader, is wrong."""


# ---- comparison ---------------------------------------------------------------------------------------------------

def norm_string(value):
    """Trimmed, whitespace collapsed, case-folded; None stays None (never "")."""
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def parse_amount(value):
    """A Decimal, or None when the value is not a plain number. Floats go through str() so 40.0 is 40.0."""
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def parse_date(value):
    """A date from an unambiguous form, or None. A year-last numeric date is never guessed."""
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def check_expected(field, value):
    """Raise BadExpected when an expected amount or date does not parse."""
    if value is None:
        return
    if field in AMOUNT_FIELDS and parse_amount(value) is None:
        raise BadExpected(f"expected {field} is not a number")
    if field in DATE_FIELDS and parse_date(value) is None:
        raise BadExpected(f"expected {field} is not an unambiguous date")


def compare_field(field, expected, actual):
    """correct | missing | unexpected | wrong, by the rules in the module docstring."""
    check_expected(field, expected)
    if expected is None and actual is None:
        return "correct"
    if actual is None:
        return "missing"
    if expected is None:
        return "unexpected"
    if field in AMOUNT_FIELDS:
        got = parse_amount(actual)
        if got is None:
            return "wrong"
        return "correct" if abs(got - parse_amount(expected)) <= AMOUNT_TOLERANCE else "wrong"
    if field in DATE_FIELDS:
        got = parse_date(actual)
        if got is not None:
            return "correct" if got == parse_date(expected) else "wrong"
        # Not a date that can be read without guessing: only the same text matches.
        return "correct" if norm_string(actual) == norm_string(expected) else "wrong"
    return "correct" if norm_string(actual) == norm_string(expected) else "wrong"


def line_key_field(expected_lines):
    for field in LINE_KEYS:
        if all(norm_string(line.get(field)) for line in expected_lines):
            return field
    raise BadExpected("no line field (" + ", ".join(LINE_KEYS) + ") is filled on every expected line")


def check_field_names(expected, fields):
    """Raise BadExpected for an expected key the target's output does not have (a typo would score missing forever).

    `fields` is (header field names, line field names) from the target's own output shape, or None to skip.
    """
    if fields is None:
        return
    header, line = (set(x) for x in fields)
    unknown = sorted(k for k in expected if k != "lines" and k not in header)
    lines = expected.get("lines") if isinstance(expected.get("lines"), list) else []
    unknown += sorted({f"lines[].{k}" for x in lines if isinstance(x, dict) for k in x if k not in line})
    if unknown:
        raise BadExpected("expected keys the reader output does not have: " + ", ".join(unknown))


def score_case(expected, actual, fields=None):
    """Score one read against its expected JSON; `fields` as in check_field_names.

    Returns {"fields": [(name, outcome), ...] in a stable order, "lines_missing", "lines_extra", "line_key",
    "exact"}. Line field names are lines[].<field>; the line set is lines[].
    """
    if not isinstance(expected, dict):
        raise BadExpected("expected JSON is not an object")
    if not isinstance(actual, dict):
        raise TypeError("the read is not an object")
    check_field_names(expected, fields)
    outcomes = []
    for field in sorted(k for k in expected if k != "lines"):
        outcomes.append((field, compare_field(field, expected[field], actual.get(field))))
    missing_lines = extra_lines = 0
    key = None
    if "lines" in expected:
        want = expected["lines"]
        if not isinstance(want, list) or not all(isinstance(x, dict) for x in want):
            raise BadExpected("expected lines is not a list of objects")
        got = actual.get("lines") or []
        if want:
            key = line_key_field(want)
        unmatched = list(got)
        line_outcomes = []
        for line in want:
            # Every expected value is checked up front, matched or not, so a bad corpus never scores.
            for field, value in line.items():
                check_expected(field, value)
            wanted = norm_string(line[key])
            match = next((n for n, other in enumerate(unmatched) if norm_string(other.get(key)) == wanted), None)
            if match is None:
                missing_lines += 1
                line_outcomes += [(f"lines[].{field}", "missing") for field in line]
                continue
            read = unmatched.pop(match)
            line_outcomes += [(f"lines[].{field}", compare_field(field, value, read.get(field)))
                              for field, value in line.items()]
        extra_lines = len(unmatched)
        outcomes += sorted(line_outcomes)
        outcomes.append((LINE_SET, "correct" if not missing_lines and not extra_lines else "wrong"))
    return {"fields": outcomes, "lines_missing": missing_lines, "lines_extra": extra_lines, "line_key": key,
            "exact": all(outcome == "correct" for _, outcome in outcomes)}


# ---- run status ---------------------------------------------------------------------------------------------------

def run_status(result):
    """(status, readers named by that status) from process()'s result, before any scoring."""
    if result.get("selected_engine", "none") != "none":
        return "read", []
    by_status = {}
    for entry in result.get("trace") or []:
        status = entry.get("status")
        if status == "failed":
            status = "timed_out" if (entry.get("ocr_status") == "timed_out"
                                     or "timed out" in str(entry.get("reason", "")).lower()) else "failed"
        elif status == "models_unavailable":
            status = "unavailable"
        by_status.setdefault(status, []).append(entry.get("engine"))
    for status in ("timed_out", "failed", "unavailable"):
        if status in by_status:
            return status, sorted(set(by_status[status]))
    # The readers that ran and gave back text or nothing: names only, never what they read.
    return "no_invoice", sorted({e for status in ("text_only", "no_fields", "extracted") for e in by_status.get(status, [])})


# ---- running ------------------------------------------------------------------------------------------------------

def is_document(path):
    return path.is_file() and path.suffix.lower() in DOCUMENT_SUFFIXES


def find_cases(corpus):
    """Cases sorted by name: [{"name", "document", "expected", "layout_error"}].

    Flat: a document with <stem>.json beside it is a case named by the document's file name; a document with no JSON
    is a layout error. Per case: a subdirectory with exactly one document and expected.json is a case named by the
    directory; anything else in a subdirectory with a document or an expected.json is a layout error for that case.
    """
    cases = []
    for path in sorted(corpus.iterdir()):
        if is_document(path):
            expected = path.with_suffix(".json")
            cases.append({"name": path.name, "document": path, "expected": expected if expected.is_file() else None,
                          "layout_error": None if expected.is_file() else "no expected JSON beside the document"})
        elif path.is_dir():
            documents = sorted(p for p in path.iterdir() if is_document(p))
            expected = path / EXPECTED_NAME
            if not documents and not expected.exists():
                continue
            error = (f"{len(documents)} documents in the case directory, need exactly 1" if len(documents) != 1
                     else None if expected.is_file() else f"no {EXPECTED_NAME} in the case directory")
            cases.append({"name": path.name + "/", "document": documents[0] if len(documents) == 1 else None,
                          "expected": expected if expected.is_file() else None, "layout_error": error})
    return cases


class LocalChain:
    """The app's own chain in this process: app.engines.process(), AI off, a fresh data root per run.

    A target gives the runner four things, so another target (a deployed HTTP endpoint) can stand in:
      name; runs (the run names it offers); fields() -> (header field names, line field names) of its output;
      readers() -> {reader: {"installed", "models_blocked", "version"}}; read(path, run) -> a run dict with status,
      detail, seconds, invoice, selected_engine, and for the chain need and unavailable_in_chain.
    """
    name = "local"
    runs = ENGINES + (CHAIN,)

    def __init__(self):
        sys.path.insert(0, str(ROOT))
        from app import engines, models
        self.engines, self.models = engines, models

    def fields(self):
        return ([f for f in self.models.Invoice.model_fields if f != "lines"], list(self.models.Line.model_fields))

    def readers(self):
        return {c["id"]: {"installed": c["installed"], "models_blocked": c["models_blocked"],
                          "version": c["version"]} for c in self.engines.capabilities()}

    @staticmethod
    def ai_reader(path, text, options):
        raise AssertionError("the scorecard runs with the AI off, but process() called the AI reader")

    def read(self, path, run):
        options = self.models.ProcessingOptions(engine=run, prefer_native_text=run == CHAIN, ai_fallback=False)
        with tempfile.TemporaryDirectory(prefix="scorecard-") as root:
            (Path(root) / "work").mkdir()
            start = time.monotonic()
            try:
                result = self.engines.process(path, options, types.SimpleNamespace(root=Path(root)), self.ai_reader)
            except Exception as e:  # reported as crashed with its class; the message can quote the document
                return {"status": "crashed", "detail": [type(e).__name__], "seconds": time.monotonic() - start}
            seconds = time.monotonic() - start
        return run_from_result(result, run, seconds)


TARGETS = {"local": LocalChain}


def run_from_result(result, run, seconds):
    """A run dict from a process()-shaped result: status first, then the read and, for the chain, the AI need."""
    status, detail = run_status(result)
    out = {"status": status, "detail": detail, "seconds": seconds, "invoice": result.get("invoice") or {},
           "selected_engine": result.get("selected_engine")}
    if run == CHAIN:
        out["need"] = ((result.get("readers") or {}).get("ai") or {}).get("need")
        out["unavailable_in_chain"] = sorted({t["engine"] for t in result.get("trace") or []
                                              if t.get("status") in ("unavailable", "models_unavailable")})
    return out


def score_run(run, expected, fields=None):
    """Attach the score to a read run; leave every other status unscored. The invoice itself is dropped."""
    if run["status"] != "read":
        return {k: v for k, v in run.items() if k != "invoice"}
    scored = score_case(expected, run["invoice"], fields)
    return {**{k: v for k, v in run.items() if k != "invoice"}, **scored}


def summarise(name, runs):
    """Per reader: status counts, read count, field accuracy over read runs, losing cases, seconds."""
    statuses = Counter(run["status"] for run in runs.values())
    read = {case: run for case, run in runs.items() if run["status"] == "read"}
    per_field = {}
    for run in read.values():
        for field, outcome in run["fields"]:
            tally = per_field.setdefault(field, Counter())
            tally[outcome] += 1
    fields = {f: {"correct": t["correct"], "total": sum(t.values()), "accuracy": ratio(t["correct"], sum(t.values())),
                  **{o: t[o] for o in ("wrong", "missing", "unexpected") if t[o]}}
              for f, t in sorted(per_field.items())}
    correct = sum(f["correct"] for f in fields.values())
    total = sum(f["total"] for f in fields.values())
    loses = {case: {"status": run["status"], **({"detail": run["detail"]} if run.get("detail") else {}),
                    **({"not_correct": sorted({f for f, o in run["fields"] if o != "correct"}),
                        "lines_missing": run["lines_missing"], "lines_extra": run["lines_extra"]}
                       if run["status"] == "read" else {})}
             for case, run in sorted(runs.items()) if run["status"] != "read" or not run["exact"]}
    times = [run["seconds"] for run in runs.values() if run["status"] not in ("bad_expected",) and "seconds" in run]
    summary = {"cases": len(runs), "statuses": {s: statuses[s] for s in STATUSES if statuses[s]},
               "read": len(read), "exact": sum(run["exact"] for run in read.values()),
               "accuracy": ratio(correct, total), "fields_correct": correct, "fields_scored": total,
               "fields": fields, "loses": loses,
               "seconds": {"mean": sum(times) / len(times), "max": max(times)} if times else None}
    if name == CHAIN:
        ran = [run for run in runs.values() if run["status"] not in ("crashed", "bad_expected")]
        summary["winners"] = dict(sorted(Counter(run["selected_engine"] for run in ran).items()))
        summary["ai_need"] = dict(sorted(Counter(str(run["need"]) for run in ran).items()))
        handover = sum(run["need"] in HANDOVER_NEEDS for run in ran)
        summary["handover"] = {"count": handover, "of": len(ran), "rate": ratio(handover, len(ran))}
        summary["unavailable_in_chain"] = sorted({e for run in ran for e in run["unavailable_in_chain"]})
    return summary


def ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def run_corpus(corpus, names, target, redact=False):
    """Every case through every named run of `target`, scored. Returns the report dict written as scorecard.json."""
    at_start = target.readers()
    fields = target.fields()
    cases = find_cases(corpus)
    label = {case["name"]: (f"case-{n:03d}" if redact else case["name"]) for n, case in enumerate(cases, 1)}
    runs = {name: {} for name in names}
    for case in cases:
        name = label[case["name"]]
        problem = case["layout_error"]
        if problem is None:
            try:
                expected = json.loads(case["expected"].read_text(encoding="utf-8"))
                score_case(expected, {}, fields)  # validates the expected JSON before any reader runs
            except BadExpected as e:
                problem = str(e)
            except json.JSONDecodeError:
                problem = "expected JSON does not parse"
        if problem is not None:
            for run in names:
                runs[run][name] = {"status": "bad_expected", "detail": [problem]}
            continue
        for run in names:
            runs[run][name] = score_run(target.read(case["document"], run), expected, fields)
    at_end = target.readers()
    return {"target": target.name, "corpus_cases": len(cases),
            "rules": {"amount_tolerance": str(AMOUNT_TOLERANCE), "amount_fields": sorted(AMOUNT_FIELDS),
                      "date_fields": sorted(DATE_FIELDS), "line_key_order": list(LINE_KEYS),
                      "handover_needs": sorted(HANDOVER_NEEDS)},
            "engines": at_start, "engines_changed_during_run": at_end != at_start,
            "summary": {run: summarise(run, runs[run]) for run in names},
            "cases": {run: dict(sorted(runs[run].items())) for run in names}}


# ---- output -------------------------------------------------------------------------------------------------------

def strip_timings(value):
    if isinstance(value, dict):
        return {k: strip_timings(v) for k, v in value.items() if k != "seconds"}
    if isinstance(value, list):
        return [strip_timings(v) for v in value]
    return value


def pct(value):
    return "n/a" if value is None else f"{100 * value:.1f}%"


def table(report):
    out = [f"Target: {report['target']}; corpus: {report['corpus_cases']} cases",
           "Readers: " + ", ".join(f"{e} {'installed ' + (c['version'] or '') if c['installed'] else 'NOT INSTALLED'}"
                                   + (" (models blocked)" if c["models_blocked"] else "")
                                   for e, c in report["engines"].items())]
    if report["engines_changed_during_run"]:
        out.append("WARNING: the installed readers changed during this run; its numbers mix two environments.")
    out.append("")
    names = list(report["summary"])
    out.append(f"{'reader':<14}{'read':>8}{'exact':>7}{'accuracy':>10}{'s/inv':>8}  other statuses")
    for name in names:
        s = report["summary"][name]
        other = ", ".join(f"{k} {v}" for k, v in s["statuses"].items() if k != "read") or "-"
        secs = f"{s['seconds']['mean']:.2f}" if s.get("seconds") else "-"
        out.append(f"{name:<14}{s['read']:>4}/{s['cases']:<3}{s['exact']:>7}{pct(s['accuracy']):>10}{secs:>8}  {other}")
    out += ["", "Field accuracy over read runs (correct/scored):"]
    fields = sorted({f for name in names for f in report["summary"][name]["fields"]})
    out.append(f"{'field':<22}" + "".join(f"{n:>14}" for n in names))
    for field in fields:
        cells = []
        for name in names:
            f = report["summary"][name]["fields"].get(field)
            cells.append(f"{f['correct']}/{f['total']}" if f else "-")
        out.append(f"{field:<22}" + "".join(f"{c:>14}" for c in cells))
    for name in names:
        s = report["summary"][name]
        if name == CHAIN:
            h = s["handover"]
            out += ["", f"auto chain (AI off): winners {s['winners'] or '-'}; AI need {s['ai_need'] or '-'}",
                    f"  would hand over to the AI: {h['count']}/{h['of']} ({pct(h['rate'])})"
                    + (f"; readers unavailable in the chain: {', '.join(s['unavailable_in_chain'])}"
                       if s["unavailable_in_chain"] else "")]
        if s["loses"]:
            out += ["", f"{name} loses:"]
            for case, why in s["loses"].items():
                if why["status"] == "read":
                    out.append(f"  {case}: wrong {', '.join(why['not_correct'])}"
                               f" (lines missing {why['lines_missing']}, extra {why['lines_extra']})")
                else:
                    out.append(f"  {case}: {why['status']}" + (f" ({', '.join(why['detail'])})"
                                                                if why.get("detail") else ""))
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--target", choices=sorted(TARGETS), default="local", help="what reads the documents")
    parser.add_argument("--out", type=Path, help="write scorecard.txt and scorecard.json here")
    parser.add_argument("--engines", default=",".join(ENGINES), help="comma-separated local readers to score alone")
    parser.add_argument("--no-chain", action="store_true", help="do not score the auto chain")
    parser.add_argument("--timeout", type=int, help="seconds per reader (sets INV_ENGINE_TIMEOUT)")
    parser.add_argument("--redact-names", action="store_true", help="name cases case-001... instead of file names")
    parser.add_argument("--no-timings", action="store_true", help="drop seconds so runs diff cleanly")
    args = parser.parse_args(argv)
    names = [e.strip() for e in args.engines.split(",") if e.strip()]
    offered = TARGETS[args.target].runs
    unknown = [e for e in names if e not in offered or e == CHAIN]
    if unknown:
        parser.error(f"unknown reader {', '.join(unknown)}; choose from {', '.join(e for e in offered if e != CHAIN)}")
    if not args.no_chain:
        names.append(CHAIN)
    if not names:
        parser.error("nothing to score")
    if not args.corpus.is_dir():
        parser.error(f"{args.corpus} is not a directory")
    if args.timeout:
        os.environ["INV_ENGINE_TIMEOUT"] = str(args.timeout)
    # The AI is off; a cross-check setting from the shell must not change what the chain records.
    os.environ["INV_STUDIO_AI_CROSS_CHECK"] = "0"
    if not find_cases(args.corpus):
        print(f"No cases in {args.corpus}: no document, and no case directory.", file=sys.stderr)
        return 2
    report = run_corpus(args.corpus, names, TARGETS[args.target](), redact=args.redact_names)
    if args.no_timings:
        report = strip_timings(report)
    text = table(report)
    sys.stdout.write(text)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "scorecard.txt").write_text(text, encoding="utf-8")
        (args.out / "scorecard.json").write_text(json.dumps(report, indent=1, sort_keys=True, default=str) + "\n",
                                                 encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
