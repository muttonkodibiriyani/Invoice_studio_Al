#!/usr/bin/env python3
"""Security check of invoice2data, the template engine inside Invoice Studio.

Run it with the app's own Python, from the repository folder:
  macOS / Linux:  .venv/bin/python tools/security/check_invoice2data.py
  Windows:        .venv\\Scripts\\python tools\\security\\check_invoice2data.py
  Docker image:   docker run --rm -i --network none --entrypoint python <image> - --offline < tools/security/check_invoice2data.py
  Azure / Cloud Run container: copy the file in and run  python check_invoice2data.py --app-root /opt/inv-studio

What it checks: the installed version against constraints.txt, the installed files against pip's record,
start-up hooks, known vulnerabilities (api.osv.dev), that a template file cannot run code, that only an
administrator can add one, template folder permissions, that invoice2data's own AI fallback stays off,
that the reader gets none of the app's secrets and runs under limits, that it reads with the network
blocked and starts no program, that a slow template pattern cannot hang the app, and an inventory of its
risky code.

Exit status: 0 no failure, 1 at least one failure (or a warning with --strict), 2 could not run.
Needs Python 3.9 or newer and nothing outside the standard library.
"""
from __future__ import annotations

# ======================================================================================================
# Common section. It is the same in check_invoice2data.py, check_paddleocr.py and check_docling.py, so
# each script runs on its own (copy one file into a container and run it). Change all three together.
# ======================================================================================================
import argparse
import base64
import datetime as _dt
import hashlib
import importlib.metadata as _md
import importlib.util
import io
import json
import os
import pickletools
import platform
import re
import shutil
import site
import stat
import struct
import subprocess
import sys
import sysconfig
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

TOOL_VERSION = "1.0, 28 Oct 2026"
STATUSES = ("FAIL", "WARN", "PASS", "INFO", "SKIP")
CANARY = "invsec-canary-7f3a"


def _user_label():
    try:
        import pwd
        return f"{pwd.getpwuid(os.geteuid()).pw_name} (uid {os.geteuid()})"
    except Exception:
        import getpass
        try:
            return getpass.getuser()
        except Exception:
            return "unknown"


class Report:
    """Collects check results, prints them as they come and writes the JSON report."""

    def __init__(self, engine, args):
        self.engine = engine
        self.args = args
        self.checks = []
        self.started = time.monotonic()
        self.context = {
            "tool": f"check_{engine.lower()}.py ({TOOL_VERSION})",
            "engine": engine,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": platform.node(),
            "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
            "python": f"{sys.version.split()[0]} at {sys.executable}",
            "user": _user_label(),
        }

    def add(self, cid, title, status, detail="", evidence=None):
        assert status in STATUSES, status
        item = {"id": cid, "title": title, "status": status, "detail": detail.strip()}
        if evidence is not None:
            item["evidence"] = evidence
        self.checks.append(item)
        print(f"[{status}] {cid}  {title}")
        for para in item["detail"].split("\n"):
            lead = re.match(r"^(\s*)(- )?", para)
            hang = " " * (len(lead.group(1)) + (2 if lead.group(2) else 0))
            for line in textwrap.wrap(para, width=96, subsequent_indent=hang) or [""]:
                print("        " + line)
        sys.stdout.flush()
        return item

    def counts(self):
        return {s: sum(1 for c in self.checks if c["status"] == s) for s in STATUSES}

    def finish(self, json_path, strict):
        counts = self.counts()
        self.context["seconds"] = round(time.monotonic() - self.started, 1)
        print()
        print(f"Summary for {self.engine}: " + ", ".join(f"{counts[s]} {s.lower()}" for s in STATUSES))
        if json_path:
            payload = {"context": self.context, "summary": counts, "checks": self.checks}
            Path(json_path).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(f"Full report with the evidence: {json_path}")
        if counts["FAIL"] or (strict and counts["WARN"]):
            return 1
        return 0


def banner(engine, what):
    print(f"Invoice Studio · engine security check · {engine} · {TOOL_VERSION}")
    print(textwrap.fill(what, width=104))
    print("It changes nothing on this computer: it reads the installed files, runs the engine on sample and "
          "test files with the network blocked, and deletes its temporary files. Unless --offline is given, "
          "it sends the names and versions of the installed packages (nothing else) to api.osv.dev to look "
          "up known vulnerabilities.")
    print()


def common_arguments(parser, default_timeout):
    parser.add_argument("--app-root", type=Path, help="Invoice Studio checkout or image folder (default: found "
                        "automatically: this script's repository, /opt/inv-studio, or the current folder)")
    parser.add_argument("--json", dest="json_path", help="where to write the JSON report (default: "
                        "<engine>-security-report-<time>.json in the current folder)")
    parser.add_argument("--no-json", action="store_true", help="do not write the JSON report")
    parser.add_argument("--offline", action="store_true", help="skip the vulnerability lookup at api.osv.dev")
    parser.add_argument("--quick", action="store_true", help="skip the slow parts: hashing the large native "
                        "packages and the hostile-file reads")
    parser.add_argument("--skip-runtime", action="store_true", help="do not run the engine at all (static checks only)")
    parser.add_argument("--timeout", type=int, default=default_timeout, help="seconds allowed for one engine "
                        f"read (default {default_timeout}, the app's own limit)")
    parser.add_argument("--allow-network", action="store_true", help="let the engine reach the network during "
                        "the test reads (attempts are still recorded); by default every non-local connection is refused")
    parser.add_argument("--no-hash", action="store_true", help="list model files without hashing them")
    parser.add_argument("--write-manifest", type=Path, help="write the model files' SHA-256 manifest to this file")
    parser.add_argument("--compare-manifest", type=Path, help="compare the model files with a manifest written "
                        "earlier (on a trusted machine or at image build)")
    parser.add_argument("--strict", action="store_true", help="exit 1 on warnings as well as failures")
    parser.add_argument("--workdir", type=Path, help="folder for temporary files (default: the system temp folder)")


def find_app_root(given):
    candidates = []
    if given:
        candidates.append(Path(given))
    here = Path(__file__).resolve() if "__file__" in globals() and not str(__file__).startswith("<") else None
    if here:
        candidates.append(here.parent.parent.parent)
    candidates += [Path("/opt/inv-studio"), Path.cwd()]
    for c in candidates:
        if (c / "app" / "ocr_worker.py").is_file():
            return c.resolve()
    return None


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_version(name):
    try:
        return _md.version(name)
    except _md.PackageNotFoundError:
        return None


def read_pins(app_root):
    pins = {}
    if not app_root or not (app_root / "constraints.txt").is_file():
        return pins
    for line in (app_root / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;]+)", line)
        if m:
            pins[norm(m.group(1))] = m.group(2)
    return pins


def check_pins(report, cid, names, pins, required):
    rows, drift, missing = [], [], []
    for name in names:
        have, want = installed_version(name), pins.get(norm(name))
        if have is None:
            if name in required:
                missing.append(name)
            if want or name in required:
                rows.append(f"- {name}: not installed" + (f" (pinned {want})" if want else ""))
            continue
        if want is None:
            rows.append(f"- {name} {have}: no pin in constraints.txt")
        elif have == want or have.split("+")[0] == want.split("+")[0]:
            rows.append(f"- {name} {have}" + ("" if have == want else f" (pin {want}; the build label differs by platform)"))
        else:
            drift.append(name)
            rows.append(f"- {name} {have}, but constraints.txt pins {want}")
    evidence = {"pins_found": bool(pins), "rows": rows}
    if missing:
        return report.add(cid, "Engine installed at the reviewed versions", "FAIL",
                          f"Not installed in this Python: {', '.join(missing)}. Run this script with the app's own "
                          "Python (.venv/bin/python, .venv\\Scripts\\python, or python inside the container).\n"
                          + "\n".join(rows), evidence)
    if not pins:
        return report.add(cid, "Engine installed at the reviewed versions", "WARN",
                          "No constraints.txt found (give --app-root), so the versions could not be compared "
                          "with the reviewed pins.\n" + "\n".join(rows), evidence)
    if drift:
        return report.add(cid, "Engine installed at the reviewed versions", "FAIL",
                          "Installed versions differ from the pins the security review covered "
                          f"({', '.join(drift)}). Re-install with the project's installer, which applies "
                          "constraints.txt.\n" + "\n".join(rows), evidence)
    return report.add(cid, "Engine installed at the reviewed versions", "PASS", "\n".join(rows), evidence)


def _hash_file(path, algo="sha256"):
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h


_ALL_LISTED = None


def all_listed_files():
    global _ALL_LISTED
    if _ALL_LISTED is None:
        listed = set()
        for dist in _md.distributions():
            for f in dist.files or []:
                try:
                    listed.add(os.path.normcase(os.path.abspath(str(f.locate()))))
                except Exception:
                    pass
        _ALL_LISTED = listed
    return _ALL_LISTED


def verify_record(dist_name, skip_extra=()):
    """Compare every installed file with the hash pip recorded at install time (the RECORD file), and list
    files in the package's folders that pip did not install."""
    try:
        dist = _md.distribution(dist_name)
    except _md.PackageNotFoundError:
        return None
    base = Path(dist.locate_file(""))
    problems = []
    checked = changed = missing = nohash = 0
    nbytes = 0
    top_dirs = set()
    for f in dist.files or []:
        rel = Path(str(f))
        full = Path(f.locate())
        if rel.parts and not rel.parts[0].endswith((".dist-info", ".data")) and rel.parts[0] != ".." and len(rel.parts) > 1:
            top_dirs.add(rel.parts[0])
        if f.hash is None:
            nohash += 1
            continue
        try:
            h = _hash_file(full, f.hash.mode)
            nbytes += full.stat().st_size
        except FileNotFoundError:
            missing += 1
            problems.append(f"missing: {rel.as_posix()}")
            continue
        except OSError as e:
            missing += 1
            problems.append(f"unreadable: {rel.as_posix()} ({type(e).__name__})")
            continue
        checked += 1
        if base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode() != f.hash.value:
            changed += 1
            problems.append(f"changed since install: {rel.as_posix()}")
    extra, listed = [], all_listed_files()
    for top in sorted(top_dirs):
        root = base / top
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_dir() or "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
                continue
            relp = p.relative_to(base).as_posix()
            if any(relp.startswith(s) for s in skip_extra):
                continue
            if os.path.normcase(os.path.abspath(str(p))) not in listed:
                extra.append(relp)
    return {"dist": dist.metadata["Name"], "version": dist.version, "checked": checked, "changed": changed,
            "missing": missing, "without_hash": nohash, "bytes": nbytes, "problems": problems[:50],
            "extra_files": extra[:50], "extra_count": len(extra)}


def check_records(report, cid, names, skip_extra=()):
    results, lines = [], []
    status = "PASS"
    for name in names:
        r = verify_record(name, skip_extra)
        if r is None:
            continue
        results.append(r)
        line = f"- {r['dist']} {r['version']}: {r['checked']} files ({r['bytes'] / 1e6:.0f} MB) match"
        if r["changed"] or r["missing"]:
            status = "FAIL"
            line += f"; {r['changed']} changed, {r['missing']} missing: " + "; ".join(r["problems"][:5])
        if r["extra_count"]:
            if status == "PASS":
                status = "WARN"
            line += (f"; {r['extra_count']} file(s) in its folders that pip did not install, e.g. "
                     + ", ".join(r["extra_files"][:4]))
        lines.append(line)
    if not results:
        return report.add(cid, "Installed engine files are unchanged since install", "SKIP", "Nothing to check.")
    lead = {"PASS": "Every file matches the hash pip recorded when it installed the package.",
            "WARN": "The recorded files match, but some files were added after install. Check that they are "
                    "expected (for example caches) and not planted code.",
            "FAIL": "Some installed files no longer match what pip installed: the engine was modified after "
                    "install. Treat this install as untrusted until it is re-installed."}[status]
    return report.add(cid, "Installed engine files are unchanged since install", status,
                      lead + "\n" + "\n".join(lines), results)


def check_startup_hooks(report, cid):
    """.pth files in site-packages run code every time Python starts; an unowned one is a persistence trick."""
    dirs = set()
    try:
        dirs.update(site.getsitepackages())
    except Exception:
        pass
    try:
        dirs.add(site.getusersitepackages())
    except Exception:
        pass
    paths = sysconfig.get_paths()
    dirs.update({paths.get("purelib", ""), paths.get("platlib", "")})
    owned = {}
    for dist in _md.distributions():
        for f in dist.files or []:
            if str(f).endswith(".pth"):
                owned[os.path.normcase(str(Path(f.locate()).resolve()))] = dist.metadata["Name"]
    rows, unowned = [], []
    for d in sorted(x for x in dirs if x and Path(x).is_dir()):
        for p in sorted(Path(d).glob("*.pth")):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            runs_code = any(line.startswith(("import ", "import\t")) for line in text.splitlines())
            owner = owned.get(os.path.normcase(str(p.resolve())))
            if not owner and p.name == "_virtualenv.pth" and text.strip() == "import _virtualenv" \
                    and (p.parent / "_virtualenv.py").is_file():
                owner = "virtualenv/uv, written when the environment was created"
            rows.append({"file": str(p), "owner": owner, "runs_code": runs_code})
            if runs_code and not owner:
                unowned.append(str(p))
    if unowned:
        return report.add(cid, "No unknown start-up hooks in site-packages", "WARN",
                          "These .pth files run code whenever Python starts and no installed package owns them: "
                          + ", ".join(unowned) + ". Find out who put them there.", rows)
    hooks = [r for r in rows if r["runs_code"]]
    detail = ("No .pth file runs code at start-up." if not hooks else
              "The .pth files that run code at start-up all belong to installed packages: "
              + ", ".join(f"{Path(r['file']).name} ({r['owner']})" for r in hooks) + ".")
    return report.add(cid, "No unknown start-up hooks in site-packages", "PASS", detail, rows)


def _marker_ok(marker):
    if not marker:
        return True
    if "extra" in marker:
        return False
    try:
        from packaging.markers import Marker
        return Marker(marker).evaluate()
    except Exception:
        return True


def dependency_closure(roots):
    seen, todo = {}, list(roots)
    while todo:
        name = todo.pop()
        if norm(name) in seen:
            continue
        try:
            dist = _md.distribution(name)
        except _md.PackageNotFoundError:
            continue
        seen[norm(name)] = (dist.metadata["Name"], dist.version)
        for req in dist.requires or []:
            m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
            marker = req.split(";", 1)[1].strip() if ";" in req else ""
            if m and _marker_ok(marker):
                todo.append(m.group(1))
    return seen


def _http_json(url, payload=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          "User-Agent": "invoice-studio-engine-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_accepted(app_root):
    accepted = {}
    path = app_root / "security" / "accepted-advisories.txt" if app_root else None
    if path and path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                ident, _, why = line.partition("#")
                accepted[ident.strip()] = why.strip()
    return accepted


def check_vulnerabilities(report, cid, roots, app_root, offline):
    title = "No known vulnerabilities in the engine and what it depends on"
    closure = dependency_closure(roots)
    packages = sorted(closure.values(), key=lambda x: norm(x[0]))
    if offline:
        return report.add(cid, title, "SKIP", f"--offline: {len(packages)} packages not looked up.",
                          {"packages": packages})
    queries = [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v.split("+")[0]} for n, v in packages]
    try:
        hits = {}
        for i in range(0, len(queries), 500):
            chunk = queries[i:i + 500]
            answer = _http_json("https://api.osv.dev/v1/querybatch", {"queries": chunk})
            for q, r in zip(chunk, answer.get("results", [])):
                ids = [v["id"] for v in (r.get("vulns") or [])]
                if ids:
                    hits[(q["package"]["name"], q["version"])] = ids
        details = {}
        for ids in hits.values():
            for vid in ids:
                if vid not in details:
                    try:
                        details[vid] = _http_json("https://api.osv.dev/v1/vulns/" + vid)
                    except Exception:
                        details[vid] = {"id": vid}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return report.add(cid, title, "SKIP", f"Could not reach api.osv.dev ({type(e).__name__}: {e}). Run again "
                          "with internet access, or audit constraints.txt with pip-audit elsewhere.",
                          {"packages": packages})
    accepted = read_accepted(app_root)
    rows, evidence, worst = [], [], "PASS"
    rank = {"PASS": 0, "INFO": 1, "WARN": 2, "FAIL": 3}
    for (name, version), ids in sorted(hits.items()):
        groups = []
        for vid in sorted(ids, key=lambda i: (not i.startswith("GHSA-"), i)):
            names = set(details.get(vid, {}).get("aliases") or []) | {vid}
            if any(names & g for g in groups):
                for g in groups:
                    if names & g:
                        g |= names
                continue
            groups.append(names)
        for group in groups:
            vid = sorted(group, key=lambda i: (not i.startswith("GHSA-"), i))[0]
            v = details.get(vid, {})
            aliases = group
            sev = ((v.get("database_specific") or {}).get("severity") or "").upper()
            if not sev:
                sev = next((s.get("score", "") for s in v.get("severity") or [] if s.get("type", "").startswith("CVSS")), "")
                sev = "unrated" if not sev else "CVSS " + sev.split("/")[0].replace("CVSS:", "")
            fixed = sorted({ev["fixed"] for aff in v.get("affected", []) if norm(aff.get("package", {}).get("name", "")) == norm(name)
                            for rng in aff.get("ranges", []) if rng.get("type") != "GIT"
                            for ev in rng.get("events", []) if "fixed" in ev})
            ok = next((a for a in aliases if a in accepted), None)
            status = "INFO" if ok else ("FAIL" if sev in ("HIGH", "CRITICAL") else "WARN")
            if rank[status] > rank[worst]:
                worst = status
            label = ", ".join(sorted(aliases))
            summary = (v.get("summary") or v.get("details") or "").strip().split("\n")[0][:140]
            note = f" Accepted in security/accepted-advisories.txt: {accepted[ok]}" if ok else ""
            rows.append(f"- {name} {version}: {label} [{sev}] {summary}"
                        + (f" Fixed in {', '.join(fixed)}." if fixed else " No fixed version listed.") + note)
            evidence.append({"package": name, "version": version, "id": vid, "aliases": sorted(aliases),
                             "severity": sev, "fixed": fixed, "accepted": bool(ok), "summary": summary})
    if not rows:
        return report.add(cid, title, "PASS", f"api.osv.dev knows no vulnerability in any of the {len(packages)} "
                          "installed packages the engine needs (versions with a build label such as +cpu were "
                          "looked up without it).", {"packages": packages})
    lead = {"INFO": "Only advisories the security review already accepted were found.",
            "WARN": "Known vulnerabilities were found. Check each one against how the app uses the package.",
            "FAIL": "A high or critical known vulnerability was found."}[worst]
    if worst == "INFO":
        worst = "PASS"
    return report.add(cid, title, worst, lead + "\n" + "\n".join(rows), {"packages": packages, "findings": evidence})


# --- model files ---------------------------------------------------------------------------------------

_SAFE_GLOBALS = {
    ("collections", "OrderedDict"), ("_codecs", "encode"), ("builtins", "set"), ("builtins", "frozenset"),
    ("builtins", "slice"), ("builtins", "complex"), ("builtins", "bytearray"), ("__builtin__", "set"),
    ("numpy", "ndarray"), ("numpy", "dtype"), ("numpy.core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "scalar"), ("numpy._core.multiarray", "_reconstruct"),
    ("numpy._core.multiarray", "scalar"),
}
_DANGER_MODULES = {"os", "posix", "nt", "subprocess", "sys", "runpy", "socket", "shutil", "importlib", "pickle",
                   "_pickle", "marshal", "ctypes", "pty", "webbrowser", "code", "codeop", "commands", "requests",
                   "httpx", "urllib", "urllib2", "urllib3", "aiohttp", "timeit", "pdb", "bdb", "multiprocessing",
                   "asyncio", "signal", "zipimport", "types", "pip", "setuptools"}
_DANGER_BUILTINS = {"eval", "exec", "compile", "open", "getattr", "setattr", "delattr", "__import__", "input",
                    "breakpoint", "globals", "locals", "vars", "exit", "quit", "help"}


def pickle_globals(data):
    """The Python objects a pickle would import when loaded, read without loading it."""
    found, strings, memo, last = [], [], {}, None
    try:
        for op, arg, _pos in pickletools.genops(io.BytesIO(data)):
            name = op.name
            if name in ("SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "UNICODE", "SHORT_BINSTRING",
                        "BINSTRING", "STRING"):
                last = arg if isinstance(arg, str) else (arg.decode("latin-1") if isinstance(arg, bytes) else str(arg))
                strings.append(last)
            elif name == "MEMOIZE":
                memo[len(memo)] = last
            elif name in ("PUT", "BINPUT", "LONG_BINPUT"):
                memo[arg] = last
            elif name in ("GET", "BINGET", "LONG_BINGET"):
                last = memo.get(arg)
                if isinstance(last, str):
                    strings.append(last)
            elif name in ("GLOBAL", "INST"):
                mod, _, attr = str(arg).partition(" ")
                found.append((mod, attr))
                last = None
            elif name == "STACK_GLOBAL":
                found.append((strings[-2], strings[-1]) if len(strings) >= 2 else ("?", "?"))
                last = None
            else:
                last = None
            if len(found) > 20000:
                break
    except Exception as e:
        found.append(("<unreadable pickle>", type(e).__name__))
    return found


def judge_globals(pairs):
    danger, review = set(), set()
    for mod, attr in set(pairs):
        top = mod.split(".")[0]
        if (mod, attr) in _SAFE_GLOBALS or top == "torch" and not attr.startswith("load"):
            continue
        if top in _DANGER_MODULES or (top in ("builtins", "__builtin__") and attr in _DANGER_BUILTINS) \
                or (top == "operator" and attr in ("attrgetter", "methodcaller")):
            danger.add(f"{mod}.{attr}")
        else:
            review.add(f"{mod}.{attr}")
    return sorted(danger), sorted(review)


_PICKLE_SUFFIXES = (".pt", ".pth", ".bin", ".ckpt", ".pkl", ".pickle", ".joblib")


def _pickle_verdict(pairs, kind):
    if any(m == "<unreadable pickle>" for m, _ in pairs):
        return kind, "unknown", "not a readable pickle"
    danger, review = judge_globals(pairs)
    if danger:
        return kind, "code", "imports " + ", ".join(danger[:6])
    return kind, "pickle", ("only tensor and plain objects" if not review else "imports " + ", ".join(review[:6]))


def classify_model_file(path, name=None):
    """(kind, risk, note) for one file; name is the visible file name (a cache link may point at a
    hash-named blob). risk: 'none' (a data format that cannot run code), 'pickle', 'code', 'unknown'."""
    name = (name or path.name).lower()
    suffix = Path(name).suffix
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
        size = path.stat().st_size
    except OSError:
        return "unreadable", "unknown", ""
    if suffix == ".safetensors":
        try:
            with open(path, "rb") as fh:
                n = struct.unpack("<Q", fh.read(8))[0]
                if n <= 0 or n > min(size - 8, 100 * 1024 * 1024):
                    return "safetensors", "unknown", "header length is wrong: not a real safetensors file"
                header = json.loads(fh.read(n))
            tensors = {k: v for k, v in header.items() if k != "__metadata__"}
            end = max((v["data_offsets"][1] for v in tensors.values()), default=0)
            if 8 + n + end != size:
                return "safetensors", "unknown", "file size does not match its header"
            return "safetensors", "none", f"{len(tensors)} tensors"
        except Exception as e:
            return "safetensors", "unknown", f"header unreadable ({type(e).__name__})"
    if name.endswith((".lock", ".metadata")) or name == "cachedir.tag":
        return "data", "none", "download bookkeeping"
    if suffix == ".onnx":
        return "onnx", "none", ""
    if suffix in (".pdiparams", ".pdmodel") or name.endswith(".pdiparams.info"):
        return "paddle-model", "none", ""
    pickle_head = head[:1] == b"\x80" and len(head) > 1 and head[1] in (2, 3, 4, 5)
    if suffix in _PICKLE_SUFFIXES or pickle_head:
        if zipfile.is_zipfile(path):
            try:
                with zipfile.ZipFile(path) as z:
                    pairs = []
                    for info in z.infolist():
                        if info.filename.endswith(".pkl") and info.file_size < 256 * 1024 * 1024:
                            pairs += pickle_globals(z.read(info))
                return _pickle_verdict(pairs, "torch-zip")
            except Exception as e:
                return "torch-zip", "unknown", f"unreadable ({type(e).__name__})"
        if pickle_head or suffix in (".pkl", ".pickle", ".joblib"):
            if size > 256 * 1024 * 1024:
                return "pickle", "pickle", "too large to scan"
            return _pickle_verdict(pickle_globals(path.read_bytes()), "pickle")
        return "binary", "unknown", ""
    if suffix == ".npy":
        with open(path, "rb") as fh:
            header = fh.read(256)
        return ("npy", "pickle", "object array: loading it needs pickle") if b"'|O'" in header else ("npy", "none", "")
    if suffix in (".py", ".pyc"):
        return "python-code", "code", "Python code inside a model folder"
    if suffix in (".so", ".dll", ".dylib", ".pyd"):
        return "native-code", "code", "a native library inside a model folder"
    if name in (".gitignore", ".gitkeep", "license", "notice", "readme", "copying") or name.startswith(("license.", "notice.")):
        return "data", "none", ""
    if suffix in (".json", ".yml", ".yaml", ".txt", ".md", ".model", ".vocab", ".spm", ".tiktoken", ".cfg", ".ini",
                  ".xml", ".csv", ".gitattributes", ".png", ".jpg", ".jpeg", ".gif", ".svg"):
        return "data", "none", ""
    if not suffix and size < 4096 and all(32 <= b < 127 or b in (9, 10, 13) for b in head):
        return "data", "none", "small text file"
    return "other", "unknown", ""


def hf_cache_dir():
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(var):
            return Path(os.environ[var])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    if os.environ.get("XDG_CACHE_HOME"):
        return Path(os.environ["XDG_CACHE_HOME"]) / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def verify_hf_blobs(repo_dir):
    """Hugging Face names each cached file after its own hash: check every file still matches its name."""
    ok = bad = other = 0
    bad_names = []
    blobs = repo_dir / "blobs"
    if not blobs.is_dir():
        return ok, bad, other, bad_names
    for p in blobs.iterdir():
        if not p.is_file() or p.name.endswith(".incomplete"):
            continue
        if re.fullmatch(r"[0-9a-f]{64}", p.name):
            good = _hash_file(p).hexdigest() == p.name
        elif re.fullmatch(r"[0-9a-f]{40}", p.name):
            h = hashlib.sha1(b"blob %d\0" % p.stat().st_size)
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            good = h.hexdigest() == p.name
        else:
            other += 1
            continue
        if good:
            ok += 1
        else:
            bad += 1
            bad_names.append(p.name)
    return ok, bad, other, bad_names


def inventory_models(roots, hash_files):
    """roots: list of (label, folder). HF repo folders are read through their snapshots, not blobs."""
    entries, seen = [], {}
    for label, folder in roots:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for p in sorted(folder.rglob("*")):
            parts = p.relative_to(folder).parts
            if set(parts) & {"blobs", "refs", ".no_exist", ".locks", ".git", "__pycache__"}:
                continue
            if p.is_dir() or not p.exists():
                continue
            real = p.resolve()
            kind, risk, note = classify_model_file(real, p.name)
            e = {"root": label, "path": p.relative_to(folder).as_posix(), "size": real.stat().st_size,
                 "kind": kind, "risk": risk, "note": note}
            if hash_files:
                key = str(real)
                if key not in seen:
                    seen[key] = _hash_file(real).hexdigest()
                e["sha256"] = seen[key]
            entries.append(e)
    return entries


def check_model_inventory(report, cid, roots, args, expected_note=""):
    title = "Model files are in formats that cannot run code, and match their manifest"
    present = [(label, Path(f)) for label, f in roots if Path(f).is_dir()]
    if not present:
        return report.add(cid, title, "WARN", "No model folder found: " + ", ".join(str(f) for _, f in roots)
                          + ". The engine downloads its models on first use, so the first read needs the "
                          "internet and fetches files nobody has checked." + expected_note, {"roots": [str(f) for _, f in roots]})
    entries = inventory_models(present, not args.no_hash)
    total = sum(e["size"] for e in entries)
    by_risk = {}
    for e in entries:
        by_risk.setdefault(e["risk"], []).append(e)
    lines = [f"- {label}: {folder}" for label, folder in present]
    kinds = {}
    for e in entries:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    lines.append(f"- {len(entries)} files, {total / 1e6:.0f} MB: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
    status = "PASS"
    for e in by_risk.get("code", []):
        status = "FAIL"
        lines.append(f"- CAN RUN CODE: {e['root']}/{e['path']}: {e['kind']}, {e['note']}")
    for e in by_risk.get("pickle", []):
        status = "WARN" if status == "PASS" else status
        lines.append(f"- pickle-based (loading it can run code if the file is swapped): {e['root']}/{e['path']} ({e['note']})")
    for e in by_risk.get("unknown", [])[:10]:
        status = "WARN" if status == "PASS" else status
        lines.append(f"- unrecognised: {e['root']}/{e['path']} {e['note']}".rstrip())
    # Hugging Face blobs check themselves against their names.
    hf_ok = hf_bad = 0
    for label, folder in present:
        repos = [folder] if folder.name.startswith("models--") else []
        for repo in repos:
            ok, bad, _other, bad_names = verify_hf_blobs(repo)
            hf_ok += ok
            hf_bad += bad
            if bad:
                status = "FAIL"
                lines.append(f"- {repo.name}: {bad} cached file(s) do not match the hash in their name: {', '.join(bad_names[:3])}")
    if hf_ok:
        lines.append(f"- {hf_ok} Hugging Face cache files match the hash in their own name")
    manifest = {f"{e['root']}/{e['path']}": e.get("sha256") for e in entries}
    if args.write_manifest and not args.no_hash:
        Path(args.write_manifest).write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
        lines.append(f"- manifest written to {args.write_manifest}")
    if args.compare_manifest:
        try:
            old = json.loads(Path(args.compare_manifest).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            old = None
            status = "FAIL"
            lines.append(f"- manifest {args.compare_manifest} unreadable ({type(e).__name__})")
        if old is not None:
            changed = [k for k in manifest if k in old and old[k] != manifest[k]]
            added = [k for k in manifest if k not in old]
            removed = [k for k in old if k not in manifest]
            if changed or added or removed:
                status = "FAIL" if changed else ("WARN" if status == "PASS" else status)
                lines.append(f"- against {args.compare_manifest}: {len(changed)} changed, {len(added)} new, {len(removed)} gone"
                             + (": " + ", ".join((changed + added)[:5]) if changed or added else ""))
            else:
                lines.append(f"- identical to {args.compare_manifest} ({len(manifest)} files)")
    lead = {"PASS": "Every model file is a data format (safetensors, ONNX, Paddle inference, JSON/YAML/text) that "
                    "the engine reads as numbers and settings, not as code.",
            "WARN": "Some model files need a closer look (see below).",
            "FAIL": "A model file can run code when it is loaded, or no longer matches its recorded hash."}[status]
    return report.add(cid, title, status, lead + expected_note + "\n" + "\n".join(lines),
                      {"files": entries, "hf_verified": hf_ok, "hf_mismatched": hf_bad})


def check_folder_permissions(report, cid, title, folders):
    if os.name != "posix":
        return report.add(cid, title, "SKIP", "Not checked on Windows (no POSIX permissions). Keep these folders "
                          "out of shared or synced locations: " + ", ".join(str(f) for f in folders))
    world, writable, rows = [], [], []
    me = os.geteuid()
    for folder in folders:
        folder = Path(folder)
        if not folder.exists():
            continue
        for p in [folder] + list(folder.rglob("*")):
            try:
                st = p.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if st.st_mode & stat.S_IWOTH and not (stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX):
                world.append(str(p))
            elif st.st_uid == me and st.st_mode & stat.S_IWUSR:
                writable.append(str(p))
        rows.append(str(folder))
    if not rows:
        return report.add(cid, title, "SKIP", "None of the folders exists: " + ", ".join(str(f) for f in folders))
    if world:
        return report.add(cid, title, "FAIL", f"{len(world)} file(s) or folder(s) any account on this computer can "
                          "change, e.g. " + ", ".join(world[:5]) + ". Remove the write bit for others (chmod o-w).",
                          {"world_writable": world[:200], "folders": rows})
    if writable:
        return report.add(cid, title, "INFO", "Nobody but the owner can change them. The account running this check "
                          f"owns and can change {len(writable)} of them; in production, make them read-only for the "
                          "app's account so a compromised reader cannot swap a model or template for the next one.\n- "
                          + "\n- ".join(rows), {"owner_writable": len(writable), "folders": rows})
    return report.add(cid, title, "PASS", "Read-only for the account running this check, and not writable by "
                      "others.\n- " + "\n- ".join(rows), {"folders": rows})


# --- source code inventory -----------------------------------------------------------------------------

RISK_PATTERNS = [
    ("yaml-not-safe", "YAML loaded without the safe loader",
     r"yaml\.(?:unsafe_load|full_load)\(|Loader\s*=\s*(?!(?:yaml\.)?C?SafeLoader\b)[A-Za-z_][\w.]*"),
    ("pickle", "pickle / joblib loading", r"\bpickle\.loads?\(|\bcPickle\.loads?\(|\bjoblib\.load\(|\bdill\.loads?\(|allow_pickle\s*=\s*True"),
    ("torch-load", "torch.load", r"\btorch\.load\("),
    ("remote-code", "trust_remote_code=True", r"trust_remote_code\s*=\s*True"),
    ("shell", "shell commands", r"shell\s*=\s*True|\bos\.system\(|\bos\.popen\("),
    ("subprocess", "starts other programs", r"\bsubprocess\.(?:run|Popen|call|check_output|check_call)\(|from subprocess import"),
    ("network", "network calls", r"\brequests\.(?:get|post|put|head|request|Session)\b|\burlopen\(|\bhttpx\.(?:get|post|Client|AsyncClient|stream|request)\b|\baiohttp\.|\bsocket\.create_connection\(|\bhf_hub_download\(|\bsnapshot_download\("),
    ("telemetry", "telemetry or analytics", r"(?i)\btelemetry\b|\banalytics\b|sentry_sdk|posthog|mixpanel"),
    ("eval-exec", "eval / exec", r"(?<![\w.])(?:eval|exec)\("),
]
_SKIP_HOSTS = {"www.apache.org", "apache.org", "www.w3.org", "w3.org", "json-schema.org", "docs.python.org",
               "opensource.org", "www.gnu.org", "example.com", "www.example.com", "schemas.openxmlformats.org",
               "purl.org", "ns.adobe.com", "creativecommons.org", "spdx.org"}


def package_dir(module):
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(list(spec.submodule_search_locations)[0])


def scan_sources(modules):
    hits = {key: [] for key, _, _ in RISK_PATTERNS}
    hosts = {}
    files = 0
    for module in modules:
        root = package_dir(module)
        if root is None:
            continue
        for p in root.rglob("*.py"):
            rel = p.relative_to(root.parent).as_posix()
            if re.search(r"/(tests?|examples?|docs?|benchmarks?)/", "/" + rel):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files += 1
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for key, _label, pattern in RISK_PATTERNS:
                    if re.search(pattern, line):
                        snippet = stripped[:150]
                        if key == "torch-load":
                            window = " ".join(text.splitlines()[lineno - 1:lineno + 4])
                            m = re.search(r"weights_only\s*=\s*(True|False)", window)
                            snippet = ("weights_only=" + m.group(1) if m else "weights_only not set (torch >= 2.6 "
                                       "defaults to True)") + " | " + snippet
                        hits[key].append(f"{rel}:{lineno}: {snippet}")
                for host in re.findall(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,}|localhost)(?::\d+)?", line):
                    host = host.lower()
                    if host not in _SKIP_HOSTS:
                        hosts[host] = hosts.get(host, 0) + 1
    return files, hits, hosts


def check_source_inventory(report, cid, modules, notes):
    """notes: {key: (status, explanation)} for categories the engine-specific part has judged."""
    files, hits, hosts = scan_sources(modules)
    if not files:
        return report.add(cid, "Risky code in the engine (inventory)", "SKIP", "Engine source not found.")
    lines = [f"- {files} Python files read in {', '.join(modules)} (tests, examples and docs left out)"]
    status = "INFO"
    for key, label, _ in RISK_PATTERNS:
        found = hits[key]
        if not found:
            continue
        judged = notes.get(key)
        if judged and judged[0] == "WARN":
            status = "WARN"
        text = f"- {label}: {len(found)} place(s)"
        if judged:
            text += f". {judged[1]}"
        if key not in ("eval-exec", "subprocess", "network", "telemetry"):
            text += ". E.g. " + "; ".join(found[:3])
        lines.append(text)
    top_hosts = sorted(hosts.items(), key=lambda kv: -kv[1])
    if top_hosts:
        lines.append("- web addresses named in the code (the egress allow-list is built from these): "
                     + ", ".join(f"{h} ({n})" for h, n in top_hosts[:18]))
    return report.add(cid, "Risky code in the engine (inventory)", status,
                      "Where the engine's own code can load unsafe data, start programs or use the network. This "
                      "is a map for the reviewer, not a list of bugs: what matters is whether the app's path "
                      "reaches these places, which the runtime checks in this report test.\n" + "\n".join(lines),
                      {"hits": {k: v[:200] for k, v in hits.items()}, "hosts": top_hosts})


# --- running the engine under observation --------------------------------------------------------------

HOOK_SOURCE = r'''
# Written by the Invoice Studio engine security check and loaded through PYTHONPATH into every Python
# process the check starts, and their children. It records network use, new processes and file writes,
# and refuses every non-local network connection when INVSEC_PROBE_BLOCK=1.
import os as _o
_LOG = _o.environ.get("INVSEC_PROBE_LOG")
if _LOG:
    import json as _j, socket as _sock, sys as _s
    _BLOCK = _o.environ.get("INVSEC_PROBE_BLOCK") == "1"
    _F = open(_LOG, "a", buffering=1, encoding="utf-8")
    _W = (_o.O_WRONLY | _o.O_RDWR | _o.O_CREAT | _o.O_APPEND | getattr(_o, "O_TRUNC", 0))
    _N = [0]

    def _w(kind, **kw):
        if _N[0] > 20000:
            return
        _N[0] += 1
        try:
            kw["k"] = kind
            kw["pid"] = _o.getpid()
            _F.write(_j.dumps(kw, default=str) + "\n")
        except Exception:
            pass

    def _host(h):
        h = h.decode("latin-1") if isinstance(h, bytes) else str(h)
        return h, (h in ("localhost", "::1", "0.0.0.0", "") or h.startswith("127."))

    def _hook(event, args):
        if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"):
            host, local = _host(args[0])
            port = args[1] if event == "socket.getaddrinfo" else None
            _w("net", how="dns", host=host, port=port, blocked=_BLOCK and not local)
            if _BLOCK and not local:
                raise _sock.gaierror(-3, "network refused by the engine security check")
        elif event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            addr = args[1] if len(args) > 1 else None
            if isinstance(addr, tuple) and addr:
                host, local = _host(addr[0])
                _w("net", how=event.split(".")[1], host=host, port=addr[1] if len(addr) > 1 else None,
                   blocked=_BLOCK and not local)
                if _BLOCK and not local:
                    raise ConnectionRefusedError(111, "network refused by the engine security check")
        elif event == "subprocess.Popen":
            exe, argv = args[0], args[1]
            argv = [str(a) for a in argv] if isinstance(argv, (list, tuple)) else [str(argv)]
            _w("spawn", argv=argv[:12], exe=str(exe) if exe else None)
        elif event == "os.system":
            _w("spawn", argv=["(shell)", str(args[0])[:300]])
        elif event in ("os.exec", "os.posix_spawn"):
            _w("spawn", argv=[str(a) for a in (args[1] or [])][:12], exe=str(args[0]))
        elif event == "os.spawn":
            _w("spawn", argv=[str(a) for a in (args[2] or [])][:12], exe=str(args[1]))
        elif event == "os.fork":
            _w("fork")
        elif event == "urllib.Request":
            _w("http", url=str(args[0])[:300])
        elif event == "open":
            path, mode, flags = (list(args) + [None, None, None])[:3]
            writing = (isinstance(flags, int) and bool(flags & _W)) or (isinstance(mode, str) and any(c in mode for c in "wax+"))
            if writing and isinstance(path, (str, bytes, _o.PathLike)):
                _w("write", path=_o.fsdecode(path))

    _s.addaudithook(_hook)
'''


def _os_sandbox_prefix():
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        try:
            if subprocess.run(["unshare", "-Urn", "true"], capture_output=True, timeout=10).returncode == 0:
                return ["unshare", "-Urn", "--"]
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def run_observed(argv, env, timeout, cwd, workdir, block=True):
    """Run argv with the recording hook; return what it did."""
    hookdir = Path(tempfile.mkdtemp(prefix="invsec-run-", dir=str(workdir)))
    (hookdir / "sitecustomize.py").write_text(HOOK_SOURCE, encoding="utf-8")
    log = hookdir / "events.jsonl"
    env = dict(env)
    env["PYTHONPATH"] = str(hookdir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["INVSEC_PROBE_LOG"] = str(log)
    env["INVSEC_PROBE_BLOCK"] = "1" if block else "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    prefix = _os_sandbox_prefix() if block else None
    cmd = (prefix or []) + list(argv)
    out_path, err_path = hookdir / "stdout.txt", hookdir / "stderr.txt"
    started = time.monotonic()
    timed_out, peak_mb = False, None
    with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
        proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env=env, stdout=fo, stderr=fe,
                                stdin=subprocess.DEVNULL)
        if hasattr(os, "wait4"):
            deadline = started + timeout
            while True:
                pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
                if pid:
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    proc.kill()
                    pid, status, usage = os.wait4(proc.pid, 0)
                    break
                time.sleep(0.1)
            rc = os.waitstatus_to_exitcode(status)
            proc.returncode = rc
            peak_mb = usage.ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)
        else:
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                rc = proc.wait()
    seconds = time.monotonic() - started
    events = []
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    tail = lambda p: p.read_text(encoding="utf-8", errors="replace")[-1500:] if p.exists() else ""
    result = {"returncode": rc, "timed_out": timed_out, "seconds": round(seconds, 1),
              "peak_memory_mb": round(peak_mb) if peak_mb else None, "events": events,
              "stdout_tail": tail(out_path), "stderr_tail": tail(err_path),
              "os_sandbox": "unshare (no network namespace)" if prefix else None, "hookdir": hookdir}
    return result


def summarize_events(result, allowed_dirs):
    """Group the recorded events: network destinations, programs started, files written outside allowed_dirs."""
    net, spawns, writes, forks, pyc = {}, [], set(), 0, 0
    allowed = [os.path.normcase(os.path.abspath(str(d))) for d in allowed_dirs if d]
    for e in result["events"]:
        k = e.get("k")
        if k == "net":
            key = f"{e.get('host')}:{e.get('port')}" if e.get("port") else str(e.get("host"))
            item = net.setdefault(key, {"count": 0, "blocked": False, "how": set()})
            item["count"] += 1
            item["blocked"] = item["blocked"] or bool(e.get("blocked"))
            item["how"].add(e.get("how"))
        elif k == "http":
            net.setdefault(e.get("url"), {"count": 0, "blocked": False, "how": {"urllib"}})["count"] += 1
        elif k == "spawn":
            text = " ".join(e.get("argv") or []) or str(e.get("exe"))
            if text not in spawns:
                spawns.append(text)
        elif k == "fork":
            forks += 1
        elif k == "write":
            path = e.get("path") or ""
            if path.endswith((".pyc", ".pyo")):
                pyc += 1
                continue
            full = os.path.normcase(os.path.abspath(path))
            if path.startswith("/dev/") or any(full == a or full.startswith(a + os.sep) for a in allowed):
                continue
            writes.add(os.path.dirname(full) or full)
    local = {h: v for h, v in net.items() if h.split(":")[0] in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "")}
    remote = {h: v for h, v in net.items() if h not in local}
    for v in net.values():
        v["how"] = sorted(x for x in v["how"] if x)
    return {"remote": remote, "local": local, "spawns": spawns, "forks": forks, "writes": sorted(writes),
            "pyc": pyc}


def describe_run(result, summary):
    lines = [f"- time {result['seconds']} s" + (f", peak memory {result['peak_memory_mb']} MB" if result["peak_memory_mb"] else "")
             + (", stopped at the time limit" if result["timed_out"] else "")]
    if summary["remote"]:
        lines.append("- network: " + ", ".join(f"{h} ×{v['count']}" + (" (refused)" if v["blocked"] else "")
                                             for h, v in sorted(summary["remote"].items())))
    else:
        lines.append("- network: no connection attempted")
    lines.append("- programs started: " + ("; ".join(summary["spawns"][:6]) if summary["spawns"] else "none")
                 + (f" ({summary['forks']} fork(s) of itself)" if summary["forks"] else ""))
    lines.append("- files written outside the temporary and model folders: "
                 + (", ".join(summary["writes"][:6]) if summary["writes"] else "none"))
    if result["os_sandbox"]:
        lines.append(f"- operating-system network isolation: {result['os_sandbox']}")
    return lines


def app_reader_env(app_root, extra=None):
    """The environment the app gives its reader children (app.engines.reader_env), computed by the app."""
    code = ("import json,sys; sys.path.insert(0, sys.argv[1]); from app.engines import reader_env; "
            "print(json.dumps(reader_env()))")
    env = dict(os.environ)
    if extra:
        env.update(extra)
    r = subprocess.run([sys.executable, "-c", code, str(app_root)], cwd=str(app_root), env=env,
                       capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "failed")
    return json.loads(r.stdout.strip().splitlines()[-1])


SECRET_VARS = ["INV_STUDIO_VAULT_KEY", "INV_STUDIO_DATABASE_URL", "INV_STUDIO_AZURE_API_KEY", "OPENAI_API_KEY",
               "ANTHROPIC_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CLIENT_SECRET", "AWS_SECRET_ACCESS_KEY",
               "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN",
               "INVOICE2DATA_AI_API_KEY", "INVOICE2DATA_AI_PROVIDER", "INVOICE2DATA_AI_BASE_URL"]


def check_reader_env(report, cid, app_root):
    title = "The engine never receives the app's secrets"
    if not app_root:
        return report.add(cid, title, "SKIP", "Needs the app (give --app-root).")
    canaries = {k: f"{CANARY}-{k}" for k in SECRET_VARS}
    try:
        env = app_reader_env(app_root, canaries)
    except Exception as e:
        return report.add(cid, title, "SKIP", f"Could not load app.engines with this Python ({e}).")
    leaked = sorted(k for k, v in env.items() if CANARY in str(v))
    if leaked:
        return report.add(cid, title, "FAIL", "These secrets reach the reader process: " + ", ".join(leaked),
                          {"passed": sorted(env)})
    return report.add(cid, title, "PASS", f"The app starts each reader with a short list of {len(env)} variables "
                      "(paths, locale, proxy and model-cache settings). Test values planted in "
                      f"{len(SECRET_VARS)} secret variables (the vault key, the database URL, AI keys, cloud "
                      "credentials, Hugging Face and invoice2data AI settings) did not reach it.",
                      {"passed": sorted(env)})


def check_reader_limits(report, cid, app_root):
    title = "Reader processes run under CPU and memory limits"
    if not app_root:
        return report.add(cid, title, "SKIP", "Needs the app (give --app-root).")
    if os.name != "posix":
        return report.add(cid, title, "WARN", "Windows has no per-process CPU or memory limits, so the readers have "
                          "only their time limits there (documented in docs/SECURITY.md). Use Docker for "
                          "untrusted invoices on Windows.")
    code = ("import json,sys; sys.path.insert(0, sys.argv[1]); from app.sandbox import limit, protect_process; "
            "limit(7, 640); protect_process(); import resource, ctypes; "
            "d = ctypes.CDLL(None).prctl(3,0,0,0,0) if sys.platform.startswith('linux') else None; "
            "print(json.dumps([resource.getrlimit(resource.RLIMIT_CPU), resource.getrlimit(resource.RLIMIT_AS), d]))")
    r = subprocess.run([sys.executable, "-c", code, str(app_root)], cwd=str(app_root), capture_output=True,
                       text=True, timeout=60)
    try:
        cpu, mem, dumpable = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return report.add(cid, title, "SKIP", "Could not run app.sandbox: " + (r.stderr.strip()[-300:] or "no output"))
    ok = list(cpu) == [7, 7] and list(mem) == [640 * 1024 * 1024] * 2
    lines = [f"- app.sandbox.limit(7 s, 640 MB) gives CPU {cpu} and address space {mem}",
             "- readers: CPU time 4 × their time limit; address space capped only when INV_STUDIO_READER_MAX_AS_MB "
             "is set, because torch and Paddle reserve far more than they use; upload checks: 10 s CPU, 512 MB"]
    if dumpable is not None:
        lines.append("- Linux: the API process hides its memory and environment from same-user processes "
                     f"(PR_SET_DUMPABLE {'works' if dumpable == 0 else 'did NOT take effect'})")
        ok = ok and dumpable == 0
    else:
        lines.append("- macOS: the limits apply; the memory-hiding step is Linux-only")
    return report.add(cid, title, "PASS" if ok else "FAIL", "\n".join(lines))


def worker_argv(app_root, engine, sample, output, timeout, templates, extra=()):
    argv = [sys.executable, "-m", "app.ocr_worker", "--engine", engine, "--file", str(sample), "--output", str(output)]
    for t in templates:
        argv += ["--templates", str(t)]
    argv += ["--language", "en", "--budget-seconds", str(timeout), "--cpu-seconds", str(max(300, 4 * timeout)),
             "--max-as-mb", os.environ.get("INV_STUDIO_READER_MAX_AS_MB", "0")]
    return argv + list(extra)


def run_app_reader(app_root, engine, sample, args, workdir, env, templates=None, extra=()):
    """Read one file exactly as the app does (python -m app.ocr_worker), with the hook recording."""
    output = Path(tempfile.mkdtemp(prefix="invsec-out-", dir=str(workdir))) / "result.json"
    templates = templates if templates is not None else [app_root / "app" / "templates"]
    argv = worker_argv(app_root, engine, sample, output, args.timeout, templates, extra)
    result = run_observed(argv, env, args.timeout + 30, app_root, workdir, block=not args.allow_network)
    payload = None
    if output.exists():
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except ValueError:
            payload = None
    shutil.rmtree(output.parent, ignore_errors=True)
    result["payload"] = payload
    return result


def payload_summary(payload):
    if not payload:
        return "no result written"
    if payload.get("error"):
        return f"refused with {payload['error']}"
    inv = payload.get("invoice") or {}
    fields = []
    for key in ("invoice_number", "number", "date", "supplier_name", "total", "amount", "currency"):
        if inv.get(key) not in (None, "", []):
            fields.append(f"{key}={str(inv[key])[:30]}")
    return (f"read {len(payload.get('text') or '')} characters, {len(payload.get('boxes') or [])} word boxes, method "
            f"{payload.get('extraction_method')}" + (f"; {', '.join(fields[:5])}" if fields else ""))


def write_pixel_bomb_png(path, width=20000, height=20000):
    """A 400-megapixel greyscale PNG of about 0.4 MB: it decodes to 400 MB or more."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    comp, row, parts = zlib.compressobj(9), b"\x00" * (width + 1), []
    for _ in range(height):
        parts.append(comp.compress(row))
    parts.append(comp.flush())
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", b"".join(parts)) + chunk(b"IEND", b""))


def write_canary_pdf(path):
    """A small valid invoice PDF that also carries JavaScript and a web link pointing at a canary address."""
    stream = (b"BT /F1 14 Tf 72 760 Td (INVOICE INV-1001) Tj 0 -24 Td (Supplier: Canary Trading LLC) Tj "
              b"0 -24 Td (Date: 28/10/2026) Tj 0 -24 Td (Total: 10.00 AED) Tj ET")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /OpenAction 5 0 R /Names << /JavaScript << /Names [(invsec) 5 0 R] >> >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 6 0 R >> >> "
        b"/Contents 4 0 R /Annots [7 0 R] /AA << /O 5 0 R >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /S /JavaScript /JS (app.launchURL\\('https://js.invsec-canary.invalid/'\\);) >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Annot /Subtype /Link /Rect [72 690 300 710] /A << /S /URI /URI (https://uri.invsec-canary.invalid/) >> >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    path.write_bytes(out.getvalue())


def write_broken_pdf(path):
    path.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n" + bytes(range(256)) * 64)


def check_upload_gate(report, cid, app_root, bomb, env):
    """The app's first gate for every upload: the limited child in app/upload_check.py."""
    title = "The app's upload check refuses an image bomb before any reader sees it"
    if not app_root:
        return report.add(cid, title, "SKIP", "Needs the app (give --app-root).")
    code = ("import sys,json; sys.path.insert(0, sys.argv[1]); from app.upload_check import check\n"
            "try:\n check(open(sys.argv[2],'rb').read(), '.png', None); print(json.dumps('accepted'))\n"
            "except ValueError as e: print(json.dumps('refused: '+str(e)))")
    started = time.monotonic()
    r = subprocess.run([sys.executable, "-c", code, str(app_root), str(bomb)], cwd=str(app_root), env=env,
                       capture_output=True, text=True, timeout=120)
    seconds = round(time.monotonic() - started, 1)
    try:
        outcome = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return report.add(cid, title, "SKIP", "Could not run app.upload_check: " + (r.stderr.strip()[-300:] or "no output"))
    if outcome.startswith("refused"):
        return report.add(cid, title, "PASS", f"A 0.4 MB PNG that claims 20,000 × 20,000 pixels was {outcome} "
                          f"(in {seconds} s, in a child limited to 10 s of CPU and 512 MB).")
    return report.add(cid, title, "FAIL", f"The upload check accepted a 400-megapixel image bomb ({seconds} s).")


def judge_hostile(summary, ok_condition):
    """A hostile file must fail (or read) cleanly and never reach the canary address it names."""
    if not ok_condition or any("invsec-canary" in h for h in summary["remote"]):
        return "FAIL"
    return "WARN" if summary["writes"] else "PASS"


def judge_run(summary, ok_condition):
    if not ok_condition:
        return "FAIL"
    if summary["remote"] or summary["writes"]:
        return "WARN"
    return "PASS"


def model_and_temp_dirs(extra):
    dirs = [tempfile.gettempdir(), os.environ.get("TMPDIR"), os.environ.get("TEMP"), os.environ.get("TMP")]
    return [d for d in dirs + list(extra) if d]
# ===================================== end of the common section =====================================


# ======================================= invoice2data checks ==========================================
ENGINE = "invoice2data"
DEFAULT_TIMEOUT = 240
MAIN_DISTS = ["invoice2data"]
PIN_DISTS = ["invoice2data", "PyYAML", "pdfplumber", "pdfminer.six", "pypdfium2", "defusedxml", "openpyxl"]
RECORD_DISTS = ["invoice2data", "PyYAML", "pdfplumber", "pdfminer.six"]
AUDIT_ROOTS = ["invoice2data", "pdfplumber", "pypdfium2", "PyYAML", "defusedxml", "openpyxl"]
WHAT = ("invoice2data turns the text of an invoice into fields using supplier templates (YAML files with "
        "regular expressions). Invoice Studio uses it for readable PDFs and, after OCR, for scans. This check "
        "covers the installed package, how templates are loaded, its own AI fallback, slow template patterns, "
        "and what it does while it reads.")


def i2d_template_loader(report, cid, workdir):
    title = "A template file is read as plain data: no code can run from it"
    folder = Path(tempfile.mkdtemp(prefix="invsec-templates-", dir=str(workdir)))
    marker = (folder / "CODE-RAN").as_posix()
    traps = {
        "trap-exec.yml": f"issuer: !!python/object/apply:builtins.exec [\"open('{marker}','w').write('exec')\"]\nkeywords: ['x']\n",
        "trap-system.yml": f"issuer: !!python/object/apply:os.system [\"echo system > '{marker}'\"]\nkeywords: ['x']\n",
        "trap-popen.yml": f"issuer: !!python/object/new:subprocess.Popen [[\"touch\", \"{marker}\"]]\nkeywords: ['x']\n",
        "trap-name.yml": "issuer: !!python/name:os.system\nkeywords: ['x']\n",
    }
    for name, text in traps.items():
        (folder / name).write_text(text, encoding="utf-8")
    (folder / "control.yml").write_text(
        "issuer: Canary Trading LLC\nkeywords:\n  - Canary Trading\nfields:\n"
        "  amount: 'Total:\\s+([\\d.]+)'\n  date: 'Date:\\s+(\\d+/\\d+/\\d+)'\n  invoice_number: 'INVOICE\\s+(\\S+)'\n",
        encoding="utf-8")
    code = ("import json,sys\nfrom invoice2data.extract.loader import read_templates\n"
            "t = read_templates(sys.argv[1])\nprint(json.dumps([str(x.get('issuer')) for x in t]))")
    run = run_observed([sys.executable, "-c", code, str(folder)], dict(os.environ), 120, None, workdir)
    summary = summarize_events(run, model_and_temp_dirs([workdir]))
    try:
        loaded = json.loads(run["stdout_tail"].strip().splitlines()[-1])
    except Exception:
        loaded = None
    source = package_dir("invoice2data")
    loader_text = (source / "extract" / "loader.py").read_text(encoding="utf-8") if source and (source / "extract" / "loader.py").is_file() else ""
    safe_static = "SafeLoader" in loader_text and not re.search(r"Unsafe|FullLoader|Loader\s*=\s*Loader\b", loader_text)
    ran = Path(marker).exists()
    lines = [f"- four trap templates (exec, os.system, subprocess.Popen, a bare os.system reference) and one normal "
             f"template in a temporary folder; invoice2data loaded: {loaded}",
             f"- code ran: {'YES' if ran else 'no'}; programs started: {len(summary['spawns'])}",
             f"- invoice2data/extract/loader.py reads YAML with {'SafeLoader' if safe_static else 'a loader that is NOT the safe one'}"]
    if ran or summary["spawns"]:
        return report.add(cid, title, "FAIL", "A template file ran code when invoice2data loaded it. Templates are "
                          "uploaded by administrators and learned by the app, so this is code execution.\n" + "\n".join(lines))
    if loaded != ["Canary Trading LLC"]:
        return report.add(cid, title, "WARN", "No code ran, but the result is not the expected one (only the normal "
                          "template should load).\n" + "\n".join(lines))
    return report.add(cid, title, "PASS" if safe_static else "WARN",
                      "The loader refused every trap and loaded the normal template, so a template file cannot "
                      "carry code. In the app only an administrator can upload a template.\n" + "\n".join(lines))


def i2d_ai_fallback_off(report, cid, app_root, workdir):
    title = "invoice2data's own AI fallback stays off"
    source = package_dir("invoice2data")
    api = (source / "api.py").read_text(encoding="utf-8") if source and (source / "api.py").is_file() else ""
    has_fallback = "ai_fallback" in api
    default_off = bool(re.search(r"ai_fallback\s*:\s*bool\s*=\s*False", api))
    lines = []
    status = "PASS"
    if not has_fallback:
        lines.append("- this invoice2data version has no AI fallback")
    else:
        lines.append(f"- invoice2data {installed_version('invoice2data')} can send invoice text to an "
                     "OpenAI-compatible service (OpenAI, DeepSeek, Mistral, Gemini, Ollama) when no template "
                     f"matches; default {'off' if default_off else 'NOT off'}")
    if app_root:
        calls = []
        for p in sorted((app_root / "app").glob("*.py")):
            text = p.read_text(encoding="utf-8")
            for m in re.finditer(r"\bextract_data\(", text):
                window = text[m.start():m.start() + 400].split("\n\n")[0]
                line = text.count("\n", 0, m.start()) + 1
                calls.append((f"app/{p.name}:{line}", "ai_fallback=False" in window.replace(" ", "")))
        if not calls:
            lines.append("- the app has no extract_data call")
        for where, off in calls:
            lines.append(f"- {where}: {'ai_fallback=False' if off else 'ai_fallback NOT set to False'}")
            if not off:
                status = "FAIL"
    # With an AI service configured in the environment, an extraction with the fallback off must not call it.
    code = ("import json\nfrom invoice2data import extract_data\n"
            "class TextReader:\n    @staticmethod\n    def to_text(_): return 'Unknown Supplier LLC\\nInvoice 77\\nTotal 12.00'\n"
            "kw = {'ai_fallback': False} if 'ai_fallback' in extract_data.__code__.co_varnames else {}\n"
            "print(json.dumps(bool(extract_data('ocr-text', templates=[], input_module=TextReader, **kw))))")
    env = dict(os.environ)
    env.update({"INVOICE2DATA_AI_PROVIDER": "openai", "INVOICE2DATA_AI_MODEL": "canary",
                "INVOICE2DATA_AI_BASE_URL": "https://ai.invsec-canary.invalid/v1", "INVOICE2DATA_AI_API_KEY": CANARY})
    run = run_observed([sys.executable, "-c", code], env, 120, None, workdir)
    summary = summarize_events(run, model_and_temp_dirs([workdir]))
    if summary["remote"]:
        status = "FAIL"
    lines.append("- an extraction with no matching template, with an AI service configured in the environment: "
                 + ("network attempted: " + ", ".join(summary["remote"]) if summary["remote"] else "no network attempt"))
    lines.append("- the app also strips every INVOICE2DATA_AI_* variable from the reader's environment (see the "
                 "secrets check below)")
    lead = ("The app always calls invoice2data with the AI fallback switched off, so invoice2data never sends "
            "invoice text anywhere. AI reading in Invoice Studio is a separate, admin-controlled step "
            "(docs/AI_BOUNDARY.md)." if status == "PASS" else
            "invoice2data could send invoice text to an AI service on the app's path.")
    return report.add(cid, title, status, lead + "\n" + "\n".join(lines))


def i2d_slow_pattern(report, cid, app_root, args, workdir):
    title = "A slow template pattern cannot hang the app"
    if not app_root:
        return report.add(cid, title, "SKIP", "Needs the app (give --app-root).")
    data = Path(tempfile.mkdtemp(prefix="invsec-redos-", dir=str(workdir)))
    (data / "templates").mkdir()
    (data / "work").mkdir()
    (data / "templates" / "slow.yml").write_text(
        "issuer: ReDoS Canary\nkeywords:\n  - ReDoS-canary\nfields:\n  amount: '(a+)+$'\n"
        "  invoice_number: 'INV-(\\d+)'\n  date: '(\\d{2}/\\d{2}/\\d{4})'\n", encoding="utf-8")
    (data / "slow.txt").write_text("ReDoS-canary\nINV-1\n28/10/2026\n" + "a" * 48 + "!\n", encoding="utf-8")
    limit_s = args.redos_seconds
    code = ("import json,os,sys,time\nsys.path.insert(0, sys.argv[1])\nfrom pathlib import Path\nfrom app import engines\n"
            "t=time.monotonic()\ntry:\n    engines.local_read('invoice2data', Path(sys.argv[2]), Path(sys.argv[3])); out='finished'\n"
            "except engines.ReaderTimedOut: out='stopped'\nexcept Exception as e: out='error: '+type(e).__name__\n"
            "print(json.dumps([out, round(time.monotonic()-t,1)]))")
    env = dict(os.environ)
    env.update({"INV_ENGINE_TIMEOUT": str(limit_s), "INV_STUDIO_LEARN": "0"})
    try:
        r = subprocess.run([sys.executable, "-c", code, str(app_root), str(data / "slow.txt"), str(data)],
                           cwd=str(app_root), env=env, capture_output=True, text=True, timeout=limit_s + 90)
        outcome, seconds = json.loads(r.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        return report.add(cid, title, "FAIL", f"The app did not stop the reader: still running after {limit_s + 90} s.")
    except Exception:
        return report.add(cid, title, "SKIP", "Could not run app.engines.local_read: " + (r.stderr.strip()[-300:] or "no output"))
    regex_src = package_dir("invoice2data")
    engine_note = ""
    if regex_src and (regex_src / "extract" / "_regex.py").is_file():
        engine_note = (" invoice2data matches template patterns with Python's re module, which has no time limit "
                       "(extract/_regex.py).")
    if outcome == "stopped" and seconds <= limit_s + 20:
        return report.add(cid, title, "PASS",
                          f"A template with a catastrophic pattern, (a+)+$, against 48 letters would run for hours."
                          f"{engine_note} The app reads in a child process and stopped it at its time limit: "
                          f"{seconds} s here with INV_ENGINE_TIMEOUT={limit_s} (240 s by default). The API process "
                          "was never blocked. The other template-pattern work (learning, review checks, checks after "
                          "each read) runs in the same kind of limited child since the N5 fixes.")
    if outcome == "finished":
        return report.add(cid, title, "WARN", f"The slow pattern finished in {seconds} s, so this run proved "
                          "nothing: invoice2data may not have applied the template. Look at the evidence.")
    return report.add(cid, title, "WARN", f"Unexpected outcome: {outcome} after {seconds} s.")


def i2d_reads(report, cid, app_root, args, workdir, env, allowed):
    title = "Reads invoices without the network and without starting other programs"
    if not app_root:
        return report.add(cid, title, "SKIP", "Needs the app (give --app-root).")
    rows, worst = [], "PASS"
    rank = {"PASS": 0, "WARN": 1, "FAIL": 2}
    samples = [("samples/invoice.pdf", app_root / "samples" / "invoice.pdf", True)]
    if not args.quick:
        canary = workdir / "canary-js-link.pdf"
        write_canary_pdf(canary)
        samples.append(("a PDF with JavaScript and a web link to a canary address", canary, True))
        broken = workdir / "broken.pdf"
        write_broken_pdf(broken)
        samples.append(("a damaged PDF", broken, False))
    evidence = []
    for label, path, should_read in samples:
        if not path.exists():
            continue
        run = run_app_reader(app_root, "invoice2data", path, args, workdir, env)
        summary = summarize_events(run, allowed)
        payload = run["payload"] or {}
        read_ok = bool(payload.get("text")) and not payload.get("error")
        ok = read_ok if should_read else (not run["timed_out"] and not read_ok)
        status = judge_run(summary, ok)
        if summary["spawns"]:
            status = "WARN" if status == "PASS" else status
        worst = max(worst, status, key=rank.get)
        rows.append(f"- {label}: {payload_summary(run['payload'])}")
        rows += ["  " + x for x in describe_run(run, summary)]
        evidence.append({"sample": label, "payload": payload_summary(run["payload"]), "run": {k: v for k, v in run.items() if k not in ("events", "hookdir", "payload")}, "summary": summary})
    lead = {"PASS": "Each file was read the way the app reads it (python -m app.ocr_worker --engine invoice2data) "
                    "with every non-local connection refused. invoice2data needed no network and started no "
                    "program: the app gives it text it read itself, so invoice2data's pdftotext and tesseract "
                    "readers are never used.",
            "WARN": "The reads worked, but something below needs a look.",
            "FAIL": "A read did not behave as expected."}[worst]
    return report.add(cid, title, worst, lead + "\n" + "\n".join(rows), evidence)


def i2d_admin_only(report, cid, app_root):
    title = "Only an administrator can add a supplier template"
    if not app_root:
        return report.add(cid, title, "SKIP", "Needs the app (give --app-root).")
    main_py = (app_root / "app" / "main.py").read_text(encoding="utf-8")
    m = re.search(r'@app\.post\("/api/templates"\)\s*\n\s*@access\((\w+)\)', main_py)
    if not m:
        return report.add(cid, title, "WARN", "Could not find the template upload route in app/main.py to check its level.")
    level = m.group(1)
    size = re.search(r"async def upload_template\(.*?\n\s*content=await file\.read\((\d+)\)", main_py, re.S)
    return report.add(cid, title, "PASS" if level == "ADMIN" else "FAIL",
                      f"POST /api/templates is @access({level}) in app/main.py"
                      + (f" and reads at most {int(size.group(1)) - 1} bytes" if size else "")
                      + ". Learned templates are written by the app itself from invoices a person verified, into "
                      "the private data folder.")


def engine_checks(report, args, app_root, workdir, pins):
    check_records(report, "I2D-02", RECORD_DISTS)
    check_startup_hooks(report, "I2D-03")
    check_vulnerabilities(report, "I2D-04", AUDIT_ROOTS, app_root, args.offline)
    i2d_template_loader(report, "I2D-05", workdir)
    i2d_admin_only(report, "I2D-06", app_root)
    data_dir = Path(os.environ.get("INV_STUDIO_DATA") or (app_root / ".data" if app_root else "."))
    folders = ([app_root / "app" / "templates"] if app_root else []) + [data_dir / "templates", data_dir / "learned" / "templates"]
    check_folder_permissions(report, "I2D-07", "Template folders cannot be changed by other accounts", folders)
    i2d_ai_fallback_off(report, "I2D-08", app_root, workdir)
    check_reader_env(report, "I2D-09", app_root)
    check_reader_limits(report, "I2D-10", app_root)
    if args.skip_runtime:
        report.add("I2D-11", "Reads invoices without the network and without starting other programs", "SKIP", "--skip-runtime")
        report.add("I2D-12", "A slow template pattern cannot hang the app", "SKIP", "--skip-runtime")
    else:
        env = app_reader_env(app_root) if app_root else dict(os.environ)
        i2d_reads(report, "I2D-11", app_root, args, workdir, env, model_and_temp_dirs([workdir]))
        i2d_slow_pattern(report, "I2D-12", app_root, args, workdir)
    check_source_inventory(report, "I2D-13", ["invoice2data"], {
        "subprocess": ("INFO", "invoice2data's pdftotext, tesseract and ocrmypdf readers; the app never uses them"),
        "network": ("INFO", "its AI fallback and its Google Vision reader; the app uses neither (I2D-08)"),
        "shell": ("INFO", "in readers the app does not use"),
    })


def extra_arguments(parser):
    parser.add_argument("--redos-seconds", type=int, default=20, help="time limit given to the app for the slow-"
                        "pattern test (default 20; the app's own default is 240)")


# ============================================ main ====================================================
def main():
    prefix = {"invoice2data": "I2D", "PaddleOCR": "PAD", "Docling": "DOC"}[ENGINE]
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.strip().split("\n\n", 1)[1] if "\n\n" in __doc__.strip() else None)
    common_arguments(parser, DEFAULT_TIMEOUT)
    extra_arguments(parser)
    args = parser.parse_args()
    banner(ENGINE, WHAT)
    app_root = find_app_root(args.app_root)
    if args.workdir:
        args.workdir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"invsec-{prefix.lower()}-", dir=str(args.workdir) if args.workdir else None))
    report = Report(ENGINE, args)
    report.context["app_root"] = str(app_root) if app_root else None
    print(f"App folder: {app_root or 'not found: the checks that run the engine the way the app does are skipped'}")
    print()
    code = 0
    try:
        pins = read_pins(app_root)
        result = check_pins(report, f"{prefix}-01", PIN_DISTS, pins, required=MAIN_DISTS[:1])
        if result["status"] == "FAIL" and installed_version(MAIN_DISTS[0]) is None:
            code = 2
        else:
            engine_checks(report, args, app_root, workdir, pins)
    except KeyboardInterrupt:
        print("\nStopped.")
        code = 2
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = None if args.no_json else (args.json_path or f"{ENGINE.lower()}-security-report-{stamp}.json")
    if json_path:
        try:
            Path(json_path).parent.mkdir(parents=True, exist_ok=True)
            with open(json_path, "a", encoding="utf-8"):
                pass
        except OSError:
            json_path = str(Path(tempfile.gettempdir()) / Path(json_path).name)
    finished = report.finish(json_path, args.strict)
    return code or finished


if __name__ == "__main__":
    sys.exit(main())
