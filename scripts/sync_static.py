#!/usr/bin/env python3
"""Derive the Firebase Hosting copy of the UI from app/static, the only place the UI is edited.

The server serves app/static directly; Firebase Hosting serves hosting/public. The two must carry
the same bytes, with this layout:

- app/static/index.html and set-password.html -> hosting/public/ (page rewrites serve them at /).
- every other app/static file -> hosting/public/static/, except firebase-auth-entry.js, which is
  the unbundled source of firebase-auth.bundle.js and is never published.

hosting/public/review (the delivery pack) and firebase.json are Hosting-only and left alone. The app never
loads /__/firebase/init.js (sign-in reads /api/public-config), so the repository keeps no copy of it.

    python3 -I scripts/sync_static.py           # copy app/static over hosting/public
    python3 -I scripts/sync_static.py --check   # exit 1 and list files if the copies differ
"""
import argparse
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGES = ("index.html", "set-password.html")
UNPUBLISHED = ("firebase-auth-entry.js",)


def mapping(root=ROOT):
    """{source file in app/static: its hosting/public destination}."""
    public = root / "hosting" / "public"
    pairs = {}
    for source in sorted((root / "app" / "static").iterdir()):
        if not source.is_file() or source.name in UNPUBLISHED:
            continue
        target = public / source.name if source.name in PAGES else public / "static" / source.name
        pairs[source] = target
    return pairs


def drift(root=ROOT):
    """Hosting files that differ from app/static, are missing, or are no longer derived from it."""
    pairs = mapping(root)
    stale = [target for source, target in pairs.items()
             if not target.exists() or target.read_bytes() != source.read_bytes()]
    derived = {target for target in pairs.values()}
    orphans = [file for file in (root / "hosting" / "public" / "static").iterdir()
               if file.is_file() and file not in derived]
    return stale, orphans


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report drift instead of copying")
    args = parser.parse_args(argv)
    stale, orphans = drift(root)
    if args.check:
        for file in stale:
            print(f"out of date: {file.relative_to(root)}")
        for file in orphans:
            print(f"not derived from app/static: {file.relative_to(root)}")
        if stale or orphans:
            print("run scripts/sync_static.py to update hosting/public from app/static")
            return 1
        print("hosting/public matches app/static")
        return 0
    for source, target in mapping(root).items():
        if not target.exists() or target.read_bytes() != source.read_bytes():
            shutil.copyfile(source, target)
            print(f"updated {target.relative_to(root)}")
    for file in orphans:
        print(f"warning: {file.relative_to(root)} is not derived from app/static; remove it by hand")
    return 0


if __name__ == "__main__":
    sys.exit(main())
