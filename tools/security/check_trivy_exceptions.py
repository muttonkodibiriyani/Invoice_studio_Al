#!/usr/bin/env python3
"""Fail a base image trivy scan on any finding not in the checked-in exception list.

container-scan runs  trivy image --format json  on each pinned base image and passes the reports here as
IMAGE=REPORT. A finding passes only when tools/security/trivy-exceptions.json has an entry with its CVE id,
its package, the scanned image's digest, and an expiry date not yet past. The job fails on:
  - any finding with no matching entry (another CVE, another package, or another image digest),
  - an entry for a digest none of the scanned images has (the base was re-pinned: remove the entry),
  - an entry past its expiry date, whether or not its finding is still there,
  - a report that is not a trivy JSON report of the image it is passed for.

  python3 tools/security/check_trivy_exceptions.py python@sha256:<64 hex>=build/trivy-0.json
  python3 tools/security/check_trivy_exceptions.py --today 2026-11-20 IMAGE=REPORT   # check an expiry

Exit status: 0 every finding excepted, 1 a finding or entry fails, 2 could not run. Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

DEFAULT_EXCEPTIONS = Path(__file__).with_name("trivy-exceptions.json")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
CVE = re.compile(r"^CVE-\d{4}-\d{4,}$")


def load_exceptions(data: dict) -> tuple[list[dict], list[str]]:
    """Return (entries, problems) for the exception list's parsed JSON; an invalid entry is a problem."""
    entries, problems = [], []
    for i, entry in enumerate(data.get("exceptions") or []):
        where = f"exception {i + 1}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: not an object")
            continue
        cve, packages, digest = entry.get("cve"), entry.get("packages"), entry.get("image_digest")
        if not isinstance(cve, str) or not CVE.match(cve):
            problems.append(f"{where}: cve {cve!r} is not a CVE id")
            continue
        where = f"exception {cve}"
        if not (isinstance(packages, list) and packages and all(isinstance(p, str) and p for p in packages)):
            problems.append(f"{where}: packages must be a non-empty list of package names")
            continue
        if not isinstance(digest, str) or not DIGEST.match(digest):
            problems.append(f"{where}: image_digest {digest!r} is not sha256:<64 hex>")
            continue
        try:
            expires = dt.date.fromisoformat(entry.get("expires"))
        except (TypeError, ValueError):
            problems.append(f"{where}: expires {entry.get('expires')!r} is not a YYYY-MM-DD date")
            continue
        if not str(entry.get("reason") or "").strip():
            problems.append(f"{where}: no reason")
            continue
        entries.append({"cve": cve, "packages": set(packages), "digest": digest, "expires": expires})
    return entries, problems


def findings(image: str, report: dict) -> tuple[list[dict], list[str]]:
    """Return (vulnerabilities, problems) for one trivy JSON report of `image`."""
    if not isinstance(report, dict) or report.get("SchemaVersion") != 2:
        return [], [f"{image}: the report is not a trivy JSON report (SchemaVersion 2)"]
    if report.get("ArtifactName") != image:
        return [], [f"{image}: the report is for {report.get('ArtifactName')!r}, not this image"]
    digest = image.rpartition("@")[2]
    repo_digests = (report.get("Metadata") or {}).get("RepoDigests") or []
    if repo_digests and not any(d.rpartition("@")[2] == digest for d in repo_digests):
        return [], [f"{image}: trivy scanned {', '.join(repo_digests)}, not {digest}"]
    vulns = []
    for result in report.get("Results") or []:
        vulns.extend(result.get("Vulnerabilities") or [])
    return vulns, []


def check(reports: dict[str, dict], exceptions: dict, today: dt.date) -> tuple[list[str], list[str]]:
    """Return (problems, notes) for trivy reports keyed by the image reference each one scanned."""
    entries, problems = load_exceptions(exceptions)
    notes: list[str] = []
    scanned = {image.rpartition("@")[2] for image in reports}
    for entry in entries:
        if entry["expires"] < today:
            problems.append(f"exception {entry['cve']} expired on {entry['expires']}: re-pin the base image to a "
                            "fixed digest, or renew the entry with a new security-review decision")
        if entry["digest"] not in scanned:
            problems.append(f"exception {entry['cve']} is for base digest {entry['digest']}, which no scanned image "
                            f"has ({', '.join(sorted(scanned)) or 'none'}): remove or update the entry")
    used: set[int] = set()
    for image, report in reports.items():
        digest = image.rpartition("@")[2]
        vulns, report_problems = findings(image, report)
        problems.extend(report_problems)
        for vuln in vulns:
            cve, pkg = vuln.get("VulnerabilityID"), vuln.get("PkgName")
            label = (f"{image}: {cve} in {pkg} {vuln.get('InstalledVersion', '?')} "
                     f"({vuln.get('Severity', '?')}, fixed in {vuln.get('FixedVersion') or '?'})")
            match = next((i for i, e in enumerate(entries)
                          if e["cve"] == cve and pkg in e["packages"] and e["digest"] == digest), None)
            if match is None:
                same_cve = [e for e in entries if e["cve"] == cve]
                why = ("the exception for this CVE does not cover this package and digest" if same_cve
                       else "not in the exception list")
                problems.append(f"{label}: {why}")
            elif entries[match]["expires"] < today:
                problems.append(f"{label}: its exception expired on {entries[match]['expires']}")
            else:
                used.add(match)
                notes.append(f"{label}: excepted until {entries[match]['expires']}")
    for i, entry in enumerate(entries):
        if i not in used and entry["digest"] in scanned and entry["expires"] >= today:
            notes.append(f"exception {entry['cve']} matched no finding: remove it once the scan stays clean")
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", nargs="+", metavar="IMAGE=REPORT",
                        help="a pinned image reference and the trivy JSON report of it")
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=None, help="YYYY-MM-DD (default: UTC today)")
    opts = parser.parse_args(argv)
    today = opts.today or dt.datetime.now(dt.timezone.utc).date()
    try:
        exceptions = json.loads(opts.exceptions.read_text(encoding="utf-8"))
        reports = {}
        for item in opts.reports:
            image, eq, path = item.partition("=")
            if not eq or "@sha256:" not in image:
                parser.error(f"{item!r} is not IMAGE=REPORT with a pinned image")
            reports[image] = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"could not read the exception list or a report: {exc}", file=sys.stderr)
        return 2
    problems, notes = check(reports, exceptions, today)
    for note in notes:
        print(note)
    for problem in problems:
        print(f"::error file=tools/security/trivy-exceptions.json::{problem}")
    if not problems:
        print(f"{len(reports)} base image report(s): every fixable HIGH/CRITICAL finding is excepted")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
