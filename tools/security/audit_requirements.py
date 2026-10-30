#!/usr/bin/env python3
"""Write the requirement list CI's dependency audit (pip-audit) reads, and its --ignore-vuln arguments.

The list is constraints.txt (every package the image installs) plus the versions the Dockerfile pins on its
own pip install lines (pip, setuptools, wheel, torch, ...), which constraints.txt does not carry or carries
with a local suffix. A local version suffix (+cpu) is dropped, because pip-audit looks the package up on
PyPI, where the same release has no suffix.

  python3 tools/security/audit_requirements.py --out build/audit-requirements.txt
  python3 tools/security/audit_requirements.py --ignore-args     # --ignore-vuln ID ... from the accepted list

Accepted advisories come from security/accepted-advisories.txt: the first word of every non-comment line.
Standard library only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PIN = re.compile(r"(?<![\w)])([A-Za-z][A-Za-z0-9_.-]*)==([0-9][A-Za-z0-9_.!+-]*)")
ARG_DEFAULT = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)\s*$", re.M)


def _strip_local(version: str) -> str:
    return version.split("+", 1)[0]


def constraint_pins(text: str) -> list[tuple[str, str]]:
    pins = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        match = PIN.fullmatch(line.split(";", 1)[0].strip()) if line else None
        if match:
            pins.append((match.group(1), _strip_local(match.group(2))))
    return pins


def dockerfile_pins(text: str) -> list[tuple[str, str]]:
    """name==version on the Dockerfile's pip install lines, with ${ARG} replaced by the ARG default."""
    defaults = {name: value.strip('"\'') for name, value in ARG_DEFAULT.findall(text)}
    joined = re.sub(r"\\\n", " ", text)
    pins = []
    for line in joined.splitlines():
        if "pip install" not in line:
            continue
        line = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda m: defaults.get(m.group(1), m.group(0)), line)
        pins += [(name, _strip_local(version)) for name, version in PIN.findall(line)]
    return pins


def requirements(constraints: str, dockerfile: str) -> list[str]:
    seen, out = set(), []
    for name, version in constraint_pins(constraints) + dockerfile_pins(dockerfile):
        key = (re.sub(r"[-_.]+", "-", name).lower(), version)
        if key not in seen:
            seen.add(key)
            out.append(f"{name}=={version}")
    return out


def accepted_ids(text: str) -> list[str]:
    ids = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.append(line.split()[0])
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository folder")
    parser.add_argument("--out", help="write the requirement list here (default: standard output)")
    parser.add_argument("--ignore-args", action="store_true", help="print --ignore-vuln arguments instead")
    opts = parser.parse_args(argv)
    root = Path(opts.root)
    if opts.ignore_args:
        accepted = root / "security" / "accepted-advisories.txt"
        ids = accepted_ids(accepted.read_text(encoding="utf-8")) if accepted.exists() else []
        print(" ".join(f"--ignore-vuln {i}" for i in ids))
        return 0
    try:
        lines = requirements((root / "constraints.txt").read_text(encoding="utf-8"),
                             (root / "Dockerfile").read_text(encoding="utf-8"))
    except OSError as exc:
        print(exc, file=sys.stderr)
        return 2
    text = "\n".join(lines) + "\n"
    if opts.out:
        Path(opts.out).parent.mkdir(parents=True, exist_ok=True)
        Path(opts.out).write_text(text, encoding="utf-8")
        print(f"{len(lines)} pinned requirements written to {opts.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
