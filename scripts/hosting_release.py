#!/usr/bin/env python3
"""Release hosting/public to a Firebase Hosting channel through the Hosting REST API.

Why not the Firebase CLI:
- A preview channel has to send /api, /health and /auth to the no-traffic candidate revision, i.e. to its Cloud Run
  tag. firebase.json cannot name a tag (the CLI only offers pinTag, which creates tags of its own on the service).
- `firebase hosting:channel:deploy` adds every channel domain to the project's Auth authorized domains, a project-wide
  setting shared with Product Intelligence. This script never calls the Auth API.

Usage (operator environment: `. ~/inv-studio-golive/tools/env.sh`):
    python3 -I scripts/hosting_release.py channel users-candidate --ttl-days 7     # create or show; prints the URL
    python3 -I scripts/hosting_release.py release users-candidate --tag candidate --message "<commit>"
    python3 -I scripts/hosting_release.py release live --live --message "<commit>"  # go-live only, after the owner's OK
    python3 -I scripts/hosting_release.py release users-candidate --tag candidate --dry-run   # show config and files

The access token comes from `gcloud auth print-access-token` (the operator config) and is never printed.
"""
import argparse
import gzip
import hashlib
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://firebasehosting.googleapis.com/v1beta1"
HOSTING = pathlib.Path(__file__).resolve().parent.parent / "hosting"
SITE = "inv-studio-740495548022"


def load_config(path):
    """firebase.json -> (public dir, ignore globs, REST ServingConfig). Accepts the CLI form (`source`, headers as a
    list of {key, value}, static rewrites as `destination`) and the REST form recorded from the live version (`glob`,
    headers as a mapping, `path`)."""
    hosting = json.loads(path.read_text())["hosting"]

    def pattern(rule):
        for field in ("glob", "source", "regex"):
            if field in rule:
                return {"regex" if field == "regex" else "glob": rule[field]}
        raise SystemExit(f"rule without source/glob/regex in {path}: {rule}")

    config = {}
    headers = []
    for rule in hosting.get("headers", []):
        values = rule["headers"]
        if isinstance(values, list):
            values = {item["key"]: item["value"] for item in values}
        headers.append({**pattern(rule), "headers": values})
    if headers:
        config["headers"] = headers
    rewrites = []
    for rule in hosting.get("rewrites", []):
        target = {"path" if key == "destination" else key: rule[key]
                  for key in ("destination", "path", "run", "dynamicLinks") if key in rule}
        if len(target) != 1:
            raise SystemExit(f"rewrite needs exactly one target in {path}: {rule}")
        rewrites.append({**pattern(rule), **target})
    if rewrites:
        config["rewrites"] = rewrites
    for key in ("redirects", "cleanUrls", "trailingSlash", "appAssociation", "i18n"):
        if key in hosting:
            raise SystemExit(f"{key} is not handled by this script; extend it before using it")
    return path.parent / hosting.get("public", "public"), hosting.get("ignore", []), config


def ignored(relative, patterns):
    if any(part.startswith(".") for part in relative.split("/")):  # covers the usual "**/.*"
        return True
    return any(pathlib.PurePosixPath(relative).match(p) or relative == p for p in patterns if p != "**/.*")


def collect(public, patterns):
    """{"/path": (sha256 of the gzipped bytes, gzipped bytes)} exactly as Hosting stores them."""
    files = {}
    for file in sorted(p for p in public.rglob("*") if p.is_file()):
        relative = file.relative_to(public).as_posix()
        if ignored(relative, patterns):
            continue
        packed = gzip.compress(file.read_bytes(), compresslevel=9, mtime=0)
        files["/" + relative] = (hashlib.sha256(packed).hexdigest(), packed)
    return files


class Hosting:
    def __init__(self, site):
        self.site = site
        self._token = subprocess.run(["gcloud", "auth", "print-access-token"], check=True, capture_output=True,
                                     text=True).stdout.strip()

    def call(self, method, url, body=None, raw=None, not_found_ok=False):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", "Bearer " + self._token)
        request.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                text = response.read().decode() or "{}"
                return json.loads(text)
        except urllib.error.HTTPError as error:
            if error.code == 404 and not_found_ok:
                return None
            detail = error.read().decode(errors="replace")[:800]
            raise SystemExit(f"{method} {url.split('?')[0]} -> HTTP {error.code}: {detail}") from None

    def channel(self, channel_id, ttl_days):
        base = f"{API}/sites/{self.site}/channels"
        found = self.call("GET", f"{base}/{channel_id}", not_found_ok=True)
        if found is None:
            query = urllib.parse.urlencode({"channelId": channel_id})
            found = self.call("POST", f"{base}?{query}", {"ttl": f"{ttl_days * 86400}s"})
        return found

    def release(self, channel_id, config, files, message):
        version = self.call("POST", f"{API}/sites/{self.site}/versions",
                            {"config": config, "labels": {"deployment-tool": "inv-studio-hosting-release"}})["name"]
        populated = self.call("POST", f"{API}/{version}:populateFiles",
                              {"files": {path: digest for path, (digest, _) in files.items()}})
        by_hash = {digest: packed for digest, packed in files.values()}
        for digest in populated.get("uploadRequiredHashes", []):
            self.call("POST", f"{populated['uploadUrl']}/{digest}", raw=by_hash[digest])
        self.call("PATCH", f"{API}/{version}?updateMask=status", {"status": "FINALIZED"})
        query = urllib.parse.urlencode({"versionName": version})
        release = self.call("POST", f"{API}/sites/{self.site}/channels/{channel_id}/releases?{query}", {"message": message})
        return version, release, len(populated.get("uploadRequiredHashes", []))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--site", default=SITE)
    parser.add_argument("--config", type=pathlib.Path, default=HOSTING / "firebase.json")
    sub = parser.add_subparsers(dest="command", required=True)
    channel = sub.add_parser("channel", help="create the preview channel if missing and print its URL")
    channel.add_argument("channel_id")
    channel.add_argument("--ttl-days", type=int, default=7)
    release = sub.add_parser("release", help="upload hosting/public as a new version and release it to a channel")
    release.add_argument("channel_id")
    release.add_argument("--tag", help="send every Cloud Run rewrite to this revision tag (preview of a candidate)")
    release.add_argument("--message", default="")
    release.add_argument("--live", action="store_true", help="required to release to the live channel")
    release.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "channel":
        if args.channel_id == "live":
            raise SystemExit("the live channel is not a preview")
        found = Hosting(args.site).channel(args.channel_id, args.ttl_days)
        print(json.dumps({key: found.get(key) for key in ("name", "url", "expireTime")}, indent=1))
        return

    if (args.channel_id == "live") != args.live:
        raise SystemExit("releasing to the live channel needs both the channel name 'live' and --live")
    if args.live and args.tag:
        raise SystemExit("the live channel must follow the service's traffic split, not a tag")
    public, patterns, config = load_config(args.config)
    for rule in config.get("rewrites", []):
        if "run" in rule and args.tag:
            rule["run"]["tag"] = args.tag
    files = collect(public, patterns)
    if args.dry_run:
        print(json.dumps(config, indent=1))
        for path, (digest, _) in files.items():
            print(digest[:12], path)
        return
    hosting = Hosting(args.site)
    if not args.live and hosting.call("GET", f"{API}/sites/{args.site}/channels/{args.channel_id}", not_found_ok=True) is None:
        raise SystemExit(f"channel {args.channel_id} does not exist; create it with the channel command first")
    version, result, uploaded = hosting.release(args.channel_id, config, files, args.message)
    print(json.dumps({"version": version, "release": result.get("name"), "files": len(files), "uploaded": uploaded},
                     indent=1))


if __name__ == "__main__":
    sys.exit(main())
