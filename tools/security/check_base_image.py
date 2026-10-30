#!/usr/bin/env python3
"""Refuse a Dockerfile whose base image is not pinned by digest.

Every FROM must name an image as  <name>@sha256:<64 hex>  (a tag before the @ is allowed and ignored by the
registry). A FROM that takes the image from a build argument (FROM ${BASE}) passes only when the argument
has no default, or a default pinned by digest; give the value the build will use with --build-arg NAME=VALUE
and it is checked too. A FROM that names an earlier stage (FROM build AS final) is allowed.

  python3 tools/security/check_base_image.py                       # Dockerfile and Dockerfile.app-layer
  python3 tools/security/check_base_image.py Dockerfile.app-layer --build-arg BASE=<image>@sha256:<digest>
  python3 tools/security/check_base_image.py --print-images        # the pinned base images, one per line

Exit status: 0 every base pinned, 1 a base not pinned, 2 could not run. Standard library only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_FILES = ("Dockerfile", "Dockerfile.app-layer")
PINNED = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
ARG_REF = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")


def logical_lines(text: str) -> list[str]:
    """Dockerfile lines with comments dropped and backslash continuations joined."""
    out, buf = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def check_dockerfile(text: str, build_args: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Return (problems, base images) for one Dockerfile's text. A base taken from a build argument with no
    default and no --build-arg value is listed as "--build-arg NAME" (the build must pass a pinned value)."""
    build_args = build_args or {}
    problems: list[str] = []
    images: list[str] = []
    args: dict[str, str | None] = {}
    stages: set[str] = set()
    for line in logical_lines(text):
        word, _, rest = line.partition(" ")
        word = word.upper()
        if word == "ARG":
            name, eq, default = rest.strip().partition("=")
            args[name.strip()] = default.strip().strip('"\'') if eq else None
            continue
        if word != "FROM":
            continue
        parts = [p for p in rest.split() if not p.startswith("--")]
        if not parts:
            problems.append(f"FROM without an image: {line!r}")
            continue
        ref = parts[0]
        if len(parts) >= 3 and parts[1].upper() == "AS":
            stages.add(parts[2].lower())
        if ref.lower() in stages or ref.lower() == "scratch":
            continue
        match = ARG_REF.match(ref)
        if match:
            name = match.group(1)
            if name in build_args:
                value, source = build_args[name], f"--build-arg {name}"
            elif args.get(name) is not None:
                value, source = args[name], f"the default of ARG {name}"
            else:
                if name not in args:
                    problems.append(f"FROM {ref}: ARG {name} is not declared before FROM")
                else:
                    images.append(f"--build-arg {name}")
                continue
            if not PINNED.match(value):
                problems.append(f"FROM {ref}: {source} is {value!r}, not pinned by digest (<image>@sha256:<64 hex>)")
            else:
                images.append(value)
            continue
        if not PINNED.match(ref):
            problems.append(f"FROM {ref}: not pinned by digest (<image>@sha256:<64 hex>)")
        else:
            images.append(ref)
    return problems, images


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="Dockerfiles (default: Dockerfile, Dockerfile.app-layer)")
    parser.add_argument("--build-arg", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--print-images", action="store_true", help="print the pinned base images and exit 0/1")
    opts = parser.parse_args(argv)
    build_args = {}
    for item in opts.build_arg:
        name, eq, value = item.partition("=")
        if not eq:
            parser.error(f"--build-arg {item!r} is not NAME=VALUE")
        build_args[name] = value
    files = opts.files or [f for f in DEFAULT_FILES if Path(f).exists()]
    if not files:
        print("no Dockerfile found", file=sys.stderr)
        return 2
    failed = False
    for name in files:
        try:
            text = Path(name).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"{name}: {exc}", file=sys.stderr)
            return 2
        problems, images = check_dockerfile(text, build_args)
        for problem in problems:
            print(f"::error file={name}::{problem}" if not opts.print_images else f"{name}: {problem}",
                  file=sys.stderr if opts.print_images else sys.stdout)
        failed = failed or bool(problems)
        pinned = [i for i in images if not i.startswith("--build-arg ")]
        unset = [i for i in images if i.startswith("--build-arg ")]
        if opts.print_images:
            for image in pinned:
                print(image)
        elif not problems:
            print(f"{name}: every base image is pinned by digest" + (f" ({', '.join(pinned)})" if pinned else "")
                  + "".join(f"; the base comes from {u}, which the build must pass pinned by digest"
                            for u in unset))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
