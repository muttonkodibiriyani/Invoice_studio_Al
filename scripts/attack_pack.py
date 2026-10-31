#!/usr/bin/env python3
"""Aggressive test pack for a deployed Invoice Studio (docs/ATTACK_PACK.md).

Runs hostile and heavy requests against the real app over HTTPS and prints one line per case: PASS, FAIL, WARN or
SKIP, with what was sent and what came back. Exit code 0 when no case failed, 1 when one did, 2 on a usage error.

    .venv/bin/python scripts/attack_pack.py --url https://<app>.<region>.azurecontainerapps.io

Tokens come from the browser (docs/AZURE.md §5): INV_ATTACK_ADMIN_TOKEN and INV_ATTACK_MEMBER_TOKEN, or a hidden
prompt. Without a token only the signed-out groups run. Every invoice the pack creates is deleted at the end
(--keep leaves them). The AI is never asked: every upload runs with AI fallback and cross-check off.
"""
import argparse
import base64
import concurrent.futures
import getpass
import io
import json
import os
import secrets
import shutil
import ssl
import socket
import statistics
import subprocess
import sys
import time
import zipfile
import zlib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
STUDIO = {"X-Studio-Request": "1"}
MAX_UPLOAD = 12_000_000
# An upload request may hold a hostile PDF for the multi-invoice check's reader limit (240 s, app/engines.py), so the
# pack waits longer than that before calling the request hung.
UPLOAD_TIMEOUT = 300
SLOW = 45  # seconds after which a refused upload still passes, but is reported as a warning
LOCAL_ONLY = {"engine": "auto", "ai_fallback": False, "ai_cross_check": False}
ENGINES = ("invoice2data", "paddleocr", "docling")
AMOUNTS = {"net", "tax", "qty", "price"}
# Words that only an internal error or a stack trace would put in a response body.
LEAKS = ("Traceback", 'File "/', "/home/", "/app/app/", "site-packages", "psycopg", "sqlite3.", "Exception:")

# Every API route that needs a sign-in, as (method, path, access). tests/test_attack_pack.py checks this list
# against the app's own routes, so a new route cannot be left out of the signed-out sweep.
ROUTES = [
    ("GET", "/api/session", "member"), ("GET", "/api/state", "member"), ("POST", "/api/preflight", "member"),
    ("POST", "/api/preflight/confirm", "member"), ("POST", "/api/preflight/renew", "member"),
    ("GET", "/api/queue", "member"), ("POST", "/api/invoices", "member"), ("POST", "/api/jobs/delete", "admin"),
    ("GET", "/api/jobs/x", "member"), ("GET", "/api/jobs/x/document", "member"),
    ("POST", "/api/jobs/x/extraction-draft", "member"), ("POST", "/api/jobs/x/review", "member"),
    ("POST", "/api/jobs/x/retry", "member"), ("POST", "/api/jobs/x/export", "member"),
    ("GET", "/api/exports/x/evidence", "member"), ("GET", "/api/target-export/config", "member"),
    ("POST", "/api/target-export/config", "admin"), ("GET", "/api/exports/x", "member"),
    ("POST", "/api/exports/extraction-batch", "member"), ("POST", "/api/exports/target-draft", "member"),
    ("POST", "/api/exports/batch", "member"), ("GET", "/api/batches/x", "member"),
    ("GET", "/api/jobs/x/target-check", "member"), ("GET", "/api/target-check/accuracy", "member"),
    ("GET", "/api/costs", "admin"), ("GET", "/api/jobs/x/audit", "member"), ("POST", "/api/references", "admin"),
    ("POST", "/api/references/demo", "admin"), ("POST", "/api/demo", "admin"),
    ("GET", "/api/samples/reference.xlsx", "member"), ("GET", "/api/samples/invoice.pdf", "member"),
    ("GET", "/api/samples/template.yml", "member"), ("POST", "/api/templates", "admin"),
    ("POST", "/api/policy", "admin"), ("POST", "/api/connections/azure", "admin"),
    ("DELETE", "/api/connections/azure", "admin"), ("POST", "/api/connections/openai", "admin"),
    ("POST", "/api/subscriptions/claude/import", "admin"), ("DELETE", "/api/subscriptions/claude", "admin"),
    ("POST", "/api/chatgpt/export", "admin"), ("POST", "/api/chatgpt/import", "admin"),
    ("GET", "/api/reference-lookup/summary", "member"), ("GET", "/api/reference-lookup/search", "member"),
    ("GET", "/api/reference-lookup/products", "member"), ("GET", "/api/fine-rules/config", "member"),
    ("POST", "/api/fine-rules/config", "admin"), ("POST", "/api/fine-rules/run", "member"),
    ("POST", "/api/fine-rules/review.xlsx", "member"), ("POST", "/api/fine-rules/target.xlsx", "member"),
    ("POST", "/api/background/x", "member"), ("GET", "/api/background/tasks/x", "member"),
    ("GET", "/api/fine-rules/feedback", "member"), ("POST", "/api/fine-rules/feedback", "member"),
    ("POST", "/api/fine-rules/feedback/x/decision", "admin"), ("GET", "/api/learned", "member"),
    ("POST", "/api/learned/jobs/x", "member"), ("DELETE", "/api/learned/x", "admin"),
    ("POST", "/api/jobs/x/draft", "member"), ("DELETE", "/api/connections/openai", "admin"),
    ("GET", "/api/connections/openai/models", "admin"), ("POST", "/api/settings", "admin"),
    ("POST", "/api/chatgpt/start", "admin"), ("POST", "/api/chatgpt/select", "admin"),
    ("DELETE", "/api/chatgpt/x", "admin"), ("GET", "/api/users", "admin"),
    ("GET", "/api/mapping-tables", "admin"), ("POST", "/api/mapping-tables/validate", "admin"),
    ("POST", "/api/mapping-tables/uploads/x/activate", "admin"),
    ("POST", "/api/mapping-tables/versions/x/rollback", "admin"),
    ("GET", "/api/mapping-tables/active/workbook", "admin"),
]
# (sample, options, exact): the five golden samples read by the whole local chain must give exactly samples/<name>.json;
# each engine forced alone must read its file. AI fallback is off, so a sample only the AI could read fails.
ENGINE_CASES = [(s, LOCAL_ONLY, True) for s in ("invoice.pdf", "invoice-2.pdf", "plain-invoice.pdf", "invoice-scan.png",
                                                "plain-invoice-scan.png")] + [
    ("invoice.pdf", {**LOCAL_ONLY, "engine": "invoice2data"}, False),
    ("invoice-scan.png", {**LOCAL_ONLY, "engine": "paddleocr"}, False),
    ("invoice-scan.png", {**LOCAL_ONLY, "engine": "docling"}, False),
    ("invoice.pdf", {**LOCAL_ONLY, "engine": "docling", "prefer_native_text": False}, False),
]
# Admin-only routes a member token tries. Each is refused before its body is read, so nothing changes either way.
MEMBER_REFUSED = [r for r in ROUTES if r[2] == "admin"]


class Pack:
    def __init__(self, url, admin=None, member=None, timeout=60, disruptive=False, keep=False, wait=900, out=print):
        self.url = url.rstrip("/")
        self.host = urlparse(self.url).netloc
        self.https = self.url.startswith("https://")
        self.admin, self.member = admin, member
        self.disruptive, self.keep, self.wait = disruptive, keep, wait
        self.http = httpx.Client(base_url=self.url, timeout=timeout, follow_redirects=False)
        self.results = []
        self.created = []  # job ids this run created, deleted at the end
        self.out = out

    # ---- plumbing -------------------------------------------------------------------------------------------------

    def record(self, group, name, status, detail=""):
        self.results.append({"group": group, "case": name, "status": status, "detail": detail})
        self.out(f"{status:<4} {group:<8} {name}" + (f"  — {detail}" if detail else ""))

    def check(self, group, name, ok, detail="", warn=False):
        self.record(group, name, "PASS" if ok else ("WARN" if warn else "FAIL"), detail)
        return ok

    def headers(self, token=None, write=False, **extra):
        h = {}
        if token:
            h["Authorization"] = "Bearer " + token
        if write:
            h.update(STUDIO)
            h["Origin"] = self.url
        h.update(extra)
        return h

    def call(self, method, path, token=None, write=None, **kw):
        write = method not in ("GET", "HEAD", "OPTIONS") if write is None else write
        headers = self.headers(token, write, **kw.pop("headers", {}))
        started = time.monotonic()
        try:
            r = self.http.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as error:
            raise ConnectionError(f"{method} {path}: {type(error).__name__} after {time.monotonic() - started:.1f}s") from None
        r.seconds = round(time.monotonic() - started, 2)
        return r

    @staticmethod
    def leaks(response):
        body = response.text[:20000]
        return [w for w in LEAKS if w in body]

    def healthy(self):
        try:
            return self.http.get("/health", timeout=15).status_code == 200
        except httpx.HTTPError:
            return False

    def guarded(self, group, name, fn):
        """One case: an exception in the pack is a FAIL with its message, never a crash of the whole run."""
        try:
            fn()
        except Exception as error:  # noqa: BLE001 - report and carry on
            self.record(group, name, "FAIL", f"{type(error).__name__}: {error}")

    # ---- edge: no sign-in ----------------------------------------------------------------------------------------

    def edge(self):
        g = "edge"
        r = self.call("GET", "/health")
        self.check(g, "health answers 200", r.status_code == 200, f"{r.status_code} in {r.seconds}s")
        cfg = self.call("GET", "/api/public-config")
        body = cfg.json() if cfg.status_code == 200 else {}
        self.config = body.get("entra") or {}
        self.check(g, "public config names an Entra tenant and app", body.get("cloud") is True and
                   bool(self.config.get("tenantId")) and bool(self.config.get("clientId")),
                   f"{cfg.status_code} cloud={body.get('cloud')}")
        secretish = [k for k in json.dumps(body).lower().split('"') if any(s in k for s in ("secret", "password", "apikey", "api_key"))]
        self.check(g, "public config carries no secret", not secretish, ",".join(secretish))

        for path, want in (("/", 200), ("/api/public-config", 200), ("/api/session", 401), ("/api/no-such-route", 404)):
            r = self.call("GET", path)
            h = r.headers
            csp = h.get("content-security-policy", "")
            script_src = next((p for p in csp.split(";") if p.strip().startswith("script-src")), "")
            missing = [n for n, ok in (
                ("status " + str(want), r.status_code == want),
                ("nosniff", h.get("x-content-type-options") == "nosniff"),
                ("referrer no-referrer", h.get("referrer-policy") == "no-referrer"),
                ("cache no-store", "no-store" in h.get("cache-control", "")),
                ("csp frame-ancestors none", "frame-ancestors 'none'" in csp),
                ("csp object-src none", "object-src 'none'" in csp),
                ("csp script-src without unsafe", "unsafe" not in script_src and bool(script_src)),
                ("hsts a year", not self.https or _hsts_seconds(h.get("strict-transport-security", "")) >= 31536000),
                ("request id", bool(h.get("x-request-id"))),
            ) if not ok]
            self.check(g, f"security headers on GET {path}", not missing, "missing: " + ", ".join(missing) if missing else "")
        server = self.call("GET", "/health").headers
        loud = {k: v for k, v in server.items() if k.lower() in ("server", "x-powered-by") and any(c.isdigit() for c in v)}
        self.check(g, "no server version banner", not loud, str(loud), warn=True)

        for path in ("/docs", "/redoc", "/openapi.json"):
            r = self.call("GET", path)
            self.check(g, f"API schema page {path} is not served", r.status_code in (404, 405) or
                       ("openapi" not in r.text.lower() and "swagger" not in r.text.lower()), str(r.status_code))
        for path in ("/static/../app/main.py", "/static/%2e%2e/%2e%2e/app/main.py", "/static/..%2f..%2fapp%2fmain.py",
                     "/.env", "/.git/config", "/static/../../.env"):
            r = self.call("GET", path)
            self.check(g, f"path traversal {path} serves no file", r.status_code in (400, 403, 404) and
                       "create_app" not in r.text and "[core]" not in r.text and "INV_STUDIO" not in r.text, str(r.status_code))
        r = self.call("GET", "/health", headers={"Host": "evil.example"})
        self.check(g, "a foreign Host header is refused", r.status_code in (400, 403, 404, 421), str(r.status_code))
        if self.https:
            self.guarded(g, "plain HTTP is not served", self.plain_http)
            self.guarded(g, "TLS 1.0 and 1.1 are refused", self.old_tls)

    def plain_http(self):
        plain = "http://" + self.host
        try:
            r = httpx.get(plain + "/api/public-config", timeout=15, follow_redirects=False)
        except httpx.HTTPError as error:
            return self.check("edge", "plain HTTP is not served", True, f"refused ({type(error).__name__})")
        location = r.headers.get("location", "")
        self.check("edge", "plain HTTP is not served", (r.status_code in (301, 302, 307, 308) and location.startswith("https://"))
                   or r.status_code in (400, 403, 404, 426), f"{r.status_code} {location}")

    def old_tls(self):
        host, _, port = self.host.partition(":")
        refused = []
        for version in (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            try:
                context.minimum_version = context.maximum_version = version
                context.set_ciphers("ALL:@SECLEVEL=0")
            except (ValueError, ssl.SSLError):
                return self.record("edge", "TLS 1.0 and 1.1 are refused", "SKIP", "this Python cannot offer old TLS")
            try:
                with socket.create_connection((host, int(port or 443)), timeout=10) as sock:
                    with context.wrap_socket(sock, server_hostname=host) as tls:
                        refused.append(f"{tls.version()} ACCEPTED")
            except (ssl.SSLError, OSError):
                refused.append(f"{version.name} refused")
        self.check("edge", "TLS 1.0 and 1.1 are refused", not any("ACCEPTED" in x for x in refused), ", ".join(refused))

    # ---- tokens --------------------------------------------------------------------------------------------------

    def tokens(self):
        g = "tokens"
        refused = []
        for method, path, _ in ROUTES:
            body = {"files": "x"} if method in ("POST",) else None
            r = self.call(method, path, write=True, json=body)
            if r.status_code != 401:
                refused.append(f"{method} {path}={r.status_code}")
        self.check(g, f"all {len(ROUTES)} signed-in routes answer 401 with no token", not refused, "; ".join(refused[:8]))
        forged = self.forged_tokens()
        for name, token in forged:
            r = self.call("GET", "/api/session", token)
            self.check(g, f"forged token refused: {name}", r.status_code == 401 and not self.leaks(r),
                       f"{r.status_code} {r.text[:80]}")
        for name, value in (("Basic scheme", "Basic YWRtaW46YWRtaW4="), ("empty Bearer", "Bearer"),
                            ("lower-case bearer", "bearer " + (forged[0][1] if forged else "x"))):
            r = self.call("GET", "/api/session", headers={"Authorization": value})
            self.check(g, f"malformed header refused: {name}", r.status_code == 401, str(r.status_code))
        r = self.call("POST", "/api/settings", "x" * 20000, json={})
        self.check(g, "a 20 KB token is refused before any parsing", r.status_code in (401, 431, 400), f"{r.status_code} in {r.seconds}s")
        graph = _az_token("https://graph.microsoft.com")
        if graph:
            r = self.call("GET", "/api/session", graph)
            self.check(g, "a real Entra token for Microsoft Graph is refused", r.status_code == 401, str(r.status_code))
        else:
            self.record(g, "a real Entra token for Microsoft Graph is refused", "SKIP", "no `az` sign-in on this machine")

    def forged_tokens(self):
        """Tokens that name this tenant and app exactly, with the admin role, but that Entra never signed."""
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        tenant, client = self.config.get("tenantId", "00000000-0000-4000-8000-000000000000"), self.config.get("clientId", "00000000-0000-4000-8000-000000000001")
        now = int(time.time())
        claims = {"iss": f"https://login.microsoftonline.com/{tenant}/v2.0", "aud": client, "tid": tenant, "ver": "2.0",
                  "iat": now, "nbf": now, "exp": now + 3600, "scp": "access_as_user", "azp": client,
                  "oid": "attack-pack-" + secrets.token_hex(4), "preferred_username": "attacker@example.test",
                  "name": "Attack pack", "roles": ["admin"]}
        kids = _tenant_kids(tenant) or ["attack-pack-kid"]
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        out = [
            ("own RSA key, real tenant kid", jwt.encode(claims, key, algorithm="RS256", headers={"kid": kids[0]})),
            ("own RSA key, unknown kid", jwt.encode(claims, key, algorithm="RS256", headers={"kid": "attack-pack-kid"})),
            ("alg none", f"{b64({'alg': 'none', 'typ': 'JWT', 'kid': kids[0]})}.{b64(claims)}."),
            ("HS256 keyed with a public key", _hs256(claims, pem, kids[0])),
            ("v1 issuer", jwt.encode({**claims, "iss": f"https://sts.windows.net/{tenant}/", "ver": "1.0"}, key,
                                     algorithm="RS256", headers={"kid": kids[0]})),
            ("garbage", "not.a.token"),
        ]
        if self.admin:
            head, body, sig = self.admin.split(".")
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            out.append(("real admin token, payload edited (oid)",
                        f"{head}.{b64({**payload, 'oid': 'attack-pack-other'})}.{sig}"))
            out.append(("real admin token, signature cut", f"{head}.{body}."))
            out.append(("real token, roles edited", f"{head}.{b64({**payload, 'roles': ['admin', 'member']})}.{sig}"))
        return out

    # ---- request protection --------------------------------------------------------------------------------------

    def csrf(self):
        g = "csrf"
        token = self.admin or self.member
        route = "/api/preflight"
        body = {"options": LOCAL_ONLY, "files": [{"name": "x.pdf", "size": 10}]}
        r = self.call("POST", route, token, write=False, json=body, headers={"Origin": self.url})
        self.check(g, "a write without X-Studio-Request is refused", r.status_code == 403, str(r.status_code))
        r = self.call("POST", route, token, json=body, headers={"Origin": "https://evil.example"})
        self.check(g, "a write from a foreign Origin is refused", r.status_code == 403, str(r.status_code))
        r = self.call("POST", route, token, json=body, headers={"Origin": "null"})
        self.check(g, "a write from Origin null is refused", r.status_code == 403, str(r.status_code))
        r = self.http.request("OPTIONS", "/api/preflight", headers={"Origin": "https://evil.example",
                                                                      "Access-Control-Request-Method": "POST"})
        allow = r.headers.get("access-control-allow-origin", "")
        self.check(g, "no CORS grant to a foreign Origin", allow not in ("*", "https://evil.example"), allow or "none")

    # ---- uploads -------------------------------------------------------------------------------------------------

    def upload(self, content, name, token=None, plan=None, options=None):
        files = {"file": (name, content, "application/octet-stream")}
        data = {"options": json.dumps(options or LOCAL_ONLY), "preflight_token": plan or ""}
        r = self.call("POST", "/api/invoices", token or self.admin, files=files, data=data, timeout=UPLOAD_TIMEOUT)
        if r.status_code == 200:
            self.created.append(r.json()["id"])
        return r

    def plan(self, files, options=None, confirm=True, token=None):
        token = token or self.admin
        r = self.call("POST", "/api/preflight", token, json={"options": options or LOCAL_ONLY,
                                                             "files": [{"name": n, "size": s} for n, s in files]})
        if r.status_code != 200:
            raise AssertionError(f"preflight {r.status_code}: {r.text[:200]}")
        plan = r.json()["token"]
        if confirm:
            c = self.call("POST", "/api/preflight/confirm", token, json={"token": plan})
            if c.status_code != 200:
                raise AssertionError(f"confirm {c.status_code}: {c.text[:200]}")
        return plan

    def uploads(self):
        """Hostile files, sent with no processing plan: the app parses and refuses them before the plan check, so a
        file it accepted as readable comes back 409 (not in a plan) and still creates no invoice."""
        g = "upload"
        pdf = (SAMPLES / "invoice.pdf").read_bytes()
        cases = [
            ("an .exe", "invoice.exe", b"MZ" + b"\0" * 200, 400),
            ("an .html page", "invoice.html", b"<html><script>alert(1)</script></html>", 400),
            ("an .svg with script", "invoice.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', 400),
            ("HTML named .pdf", "invoice.pdf", b"<html><script>alert(1)</script></html>", 400),
            ("an empty file", "invoice.pdf", b"", 400),
            ("a PDF header and nothing else", "invoice.pdf", b"%PDF-1.7\n%%EOF", 400),
            ("an encrypted-looking broken PDF", "invoice.pdf", b"%PDF-1.4\n1 0 obj<</Filter/Standard>>endobj\ntrailer<</Encrypt 1 0 R>>\n%%EOF", 400),
            ("a 21-page PDF", "invoice.pdf", _pdf_pages(21), 400),
            ("a PDF whose page draws itself 2^40 times", "invoice.pdf", _pdf_xobject_bomb(40), (400, 409)),
            ("a PDF with a 1 GB inflate bomb", "invoice.pdf", _pdf_inflate_bomb(), (400, 409)),
            ("a 36-megapixel PNG (25 MP cap)", "invoice.png", _png(6000, 6000), 400),
            ("a 400-megapixel PNG decompression bomb", "invoice.png", _png(20000, 20000), 400),
            ("a PNG named .jpg with a broken body", "invoice.jpg", b"\x89PNG\r\n\x1a\n" + os.urandom(2000), 400),
            ("a 25-frame TIFF (20 frame cap)", "invoice.tiff", _tiff_frames(25), 400),
            ("a DOCX that expands past 30 MB", "invoice.docx", _zip_bomb("word/document.xml", 31_000_000), 400),
            ("an XLSX with no workbook", "invoice.xlsx", _zip_bomb("xl/other.xml", 1000), 400),
            ("a CSV that is not UTF-8", "invoice.csv", "Rechnung;Größe\n".encode("latin-1"), 400),
            ("a valid PDF with no plan", "invoice.pdf", pdf, 409),
        ]
        for name, filename, content, want in cases:
            def case(name=name, filename=filename, content=content, want=want):
                r = self.upload(content, filename)
                wants = want if isinstance(want, tuple) else (want,)
                ok = r.status_code in wants and not self.leaks(r)
                self.check(g, f"refused: {name}", ok and r.seconds < SLOW, f"{r.status_code} in {r.seconds}s {r.text[:90]}",
                           warn=ok)
            self.guarded(g, f"refused: {name}", case)
        self.check(g, "the app is healthy after the hostile uploads", self.healthy())
        big = os.urandom(MAX_UPLOAD + 300_000)
        r = self.upload(b"%PDF-1.7\n" + big, "invoice.pdf")
        self.check(g, "a 12.3 MB upload is refused as too large", r.status_code == 413, f"{r.status_code} in {r.seconds}s")
        self.guarded(g, "a file named with ../ is stored by its base name only", self.traversal_name)

    def traversal_name(self):
        g = "upload"
        pdf = (SAMPLES / "invoice.pdf").read_bytes()
        name = "../../../../etc/attack-pack-<img src=x onerror=alert(1)>.pdf"
        plan = self.plan([(name, len(pdf))])
        r = self.upload(pdf, name, plan=plan)
        if not self.check(g, "a file named with ../ is accepted under a plan", r.status_code == 200, f"{r.status_code} {r.text[:120]}"):
            return
        job = r.json()
        self.check(g, "a file named with ../ is stored by its base name only", "/" not in job["filename"] and ".." not in job["filename"]
                   and "path" not in job, job["filename"])
        again = self.upload(pdf, name, plan=plan)
        self.check("plan", "a plan's file cannot be uploaded twice", again.status_code == 409, str(again.status_code))
        doc = self.wait_job(job["id"])
        r = self.call("GET", f"/api/jobs/{job['id']}/document", self.admin)
        ctype = r.headers.get("content-type", "")
        self.check(g, "the stored document comes back as a PDF, not HTML", r.status_code == 200 and ctype.startswith("application/pdf")
                   and r.headers.get("x-content-type-options") == "nosniff" and "frame-ancestors 'none'" in r.headers.get("content-security-policy", ""),
                   f"{r.status_code} {ctype} {r.headers.get('content-disposition', '')[:80]}")
        self.check(g, "the job of the odd file name finished", doc.get("status") not in ("queued", "processing"), doc.get("status", ""))

    # ---- processing plans ----------------------------------------------------------------------------------------

    def plans(self):
        g = "plan"
        pdf = (SAMPLES / "invoice.pdf").read_bytes()
        unconfirmed = self.plan([("p.pdf", len(pdf))], confirm=False)
        r = self.upload(pdf, "p.pdf", plan=unconfirmed)
        self.check(g, "an unconfirmed plan admits nothing", r.status_code == 409, str(r.status_code))
        plan = self.plan([("p.pdf", len(pdf) + 1)])
        r = self.upload(pdf, "p.pdf", plan=plan)
        self.check(g, "a file whose size differs from the plan is refused", r.status_code == 409, str(r.status_code))
        plan = self.plan([("p.pdf", len(pdf))])
        r = self.upload(pdf, "p.pdf", plan=plan, options={**LOCAL_ONLY, "engine": "docling"})
        self.check(g, "options changed after confirming are refused", r.status_code == 409, str(r.status_code))
        r = self.upload(pdf, "other-name.pdf", plan=plan)
        self.check(g, "a file not named in the plan is refused", r.status_code == 409, str(r.status_code))
        r = self.upload(pdf, "p.pdf", plan=secrets.token_urlsafe(32))
        self.check(g, "a made-up plan token is refused", r.status_code == 409, str(r.status_code))
        r = self.call("POST", "/api/preflight/confirm", self.admin, json={"token": "x" * 5000})
        self.check(g, "confirming a made-up plan is refused", r.status_code in (409, 422), str(r.status_code))
        if self.member:
            r = self.upload(pdf, "p.pdf", token=self.member, plan=plan)
            self.check(g, "another user's confirmed plan is not theirs to use", r.status_code == 409, f"{r.status_code}", warn=True)
        bad = [
            ("501 files in one plan", {"options": LOCAL_ONLY, "files": [{"name": f"{n}.pdf", "size": 1} for n in range(501)]}),
            ("a file over 12 MB in the plan", {"options": LOCAL_ONLY, "files": [{"name": "a.pdf", "size": MAX_UPLOAD + 1}]}),
            ("a zero-byte file in the plan", {"options": LOCAL_ONLY, "files": [{"name": "a.pdf", "size": 0}]}),
            ("an unknown engine", {"options": {**LOCAL_ONLY, "engine": "../../bin/sh"}, "files": [{"name": "a.pdf", "size": 1}]}),
            ("an unknown provider", {"options": {**LOCAL_ONLY, "provider": "evil"}, "files": [{"name": "a.pdf", "size": 1}]}),
            ("a model name starting with -", {"options": {**LOCAL_ONLY, "model": "--config=/etc/passwd"}, "files": [{"name": "a.pdf", "size": 1}]}),
            ("an extra field (mass assignment)", {"options": {**LOCAL_ONLY, "role": "admin"}, "files": [{"name": "a.pdf", "size": 1}]}),
            ("a 301-character file name", {"options": LOCAL_ONLY, "files": [{"name": "a" * 297 + ".pdf", "size": 1}]}),
            ("a size of 1e309", None),
        ]
        for name, body in bad:
            content = json.dumps(body) if body else '{"options":{},"files":[{"name":"a.pdf","size":1e309}]}'
            r = self.call("POST", "/api/preflight", self.admin, content=content, headers={"Content-Type": "application/json"})
            self.check(g, f"preflight refuses {name}", r.status_code == 422 and not self.leaks(r), f"{r.status_code} {r.text[:80]}")
        for name, content in (("broken JSON", "{"), ("a 10 MB JSON array", "[" + "1," * 5_000_000 + "1]"),
                              ("JSON nested 100000 deep", "[" * 100000 + "]" * 100000)):
            r = self.call("POST", "/api/preflight", self.admin, content=content, headers={"Content-Type": "application/json"})
            self.check(g, f"preflight refuses {name}", 400 <= r.status_code < 500 and not self.leaks(r) and r.seconds < 30,
                       f"{r.status_code} in {r.seconds}s")
        self.check(g, "the app is healthy after the plan abuse", self.healthy())

    # ---- authorization ------------------------------------------------------------------------------------------

    def authz(self):
        g = "authz"
        token = self.admin or self.member
        r = self.call("GET", "/api/session", token)
        self.check(g, "the given token signs in", r.status_code == 200, f"{r.status_code} role={r.json().get('role') if r.status_code == 200 else '-'}")
        if self.admin:
            role = r.json().get("role") if r.status_code == 200 else None
            self.check(g, "the admin token carries the admin role", role == "admin", str(role))
        if self.member:
            r = self.call("GET", "/api/session", self.member)
            self.check(g, "the member token carries the member role", r.status_code == 200 and r.json().get("role") == "member",
                       f"{r.status_code} {r.text[:60]}")
            wrong = []
            for method, path, _ in MEMBER_REFUSED:
                r = self.call(method, path, self.member, json={})
                if r.status_code != 403:
                    wrong.append(f"{method} {path}={r.status_code}")
            self.check(g, f"all {len(MEMBER_REFUSED)} admin routes refuse a member", not wrong, "; ".join(wrong[:8]))
        else:
            self.record(g, "admin routes refuse a member", "SKIP", "no member token (INV_ATTACK_MEMBER_TOKEN)")
        for jid in ("0" * 32, secrets.token_hex(16), "..%2f..%2fetc%2fpasswd", "%00", "' OR '1'='1", "1;DROP TABLE jobs"):
            r = self.call("GET", f"/api/jobs/{jid}", token)
            self.check(g, f"a made-up invoice id {jid[:24]!r} is not found", r.status_code in (404, 422) and not self.leaks(r),
                       f"{r.status_code} {r.text[:60]}")
        for q in ("' OR 1=1 --", "%' UNION SELECT password FROM users --", "\\", "*" * 5000):
            r = self.call("GET", "/api/reference-lookup/search", token, params={"kind": "item", "q": q})
            self.check(g, f"lookup search survives {q[:20]!r}", r.status_code < 500 and not self.leaks(r), f"{r.status_code} in {r.seconds}s")
        if self.admin:
            r = self.call("GET", "/api/state", self.admin)
            text = r.text
            found = [w for w in ("sk-", "api_key\": \"", "-----BEGIN", "AccountKey=", "password") if w in text]
            self.check(g, "the state answer carries no secret", r.status_code == 200 and not found, ",".join(found))
            settings = r.json().get("settings", {}) if r.status_code == 200 else {}
            r = self.call("POST", "/api/settings", self.admin, json={**settings, "is_admin": True})
            self.check(g, "settings refuse an extra field (mass assignment)", r.status_code == 422, str(r.status_code))
            r = self.call("POST", "/api/jobs/delete", self.admin, json={"jobs": [{"id": "0" * 32, "revision": 1}]})
            self.check(g, "deleting needs the explicit confirm flag", r.status_code == 400, str(r.status_code))
            r = self.call("POST", "/api/jobs/delete", self.admin, json={"jobs": [{"id": "0" * 32, "revision": 1}], "confirm_permanent": "true"})
            self.check(g, "the confirm flag must be a real boolean", r.status_code == 422, str(r.status_code))

    # ---- load ----------------------------------------------------------------------------------------------------

    def load(self, workers=20, requests=200):
        g = "load"
        token = self.admin or self.member

        def one(_):
            started = time.monotonic()
            try:
                r = self.http.get("/api/state", headers=self.headers(token), timeout=60)
                return r.status_code, time.monotonic() - started
            except httpx.HTTPError as error:
                return type(error).__name__, time.monotonic() - started

        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            got = list(pool.map(one, range(requests)))
        codes = {}
        for code, _ in got:
            codes[code] = codes.get(code, 0) + 1
        times = sorted(t for _, t in got)
        p95 = times[int(len(times) * 0.95) - 1]
        self.check(g, f"{requests} state reads, {workers} at a time, no server error", set(codes) <= {200, 429},
                   f"codes {codes}, median {statistics.median(times):.2f}s, p95 {p95:.2f}s")
        self.check(g, "p95 under 10 s under that load", p95 < 10, f"{p95:.2f}s", warn=True)
        started = time.monotonic()
        self.check(g, "health answers during the load's tail", self.healthy(), f"{time.monotonic() - started:.2f}s")

    # ---- engines -------------------------------------------------------------------------------------------------

    def engines(self):
        g = "engines"
        r = self.call("GET", "/api/state", self.admin)
        caps = {c.get("id"): c for c in r.json().get("engines", [])} if r.status_code == 200 else {}
        for engine in ENGINES:
            c = caps.get(engine, {})
            self.check(g, f"{engine} is installed in the deployed image", bool(c.get("installed")), json.dumps(c)[:160])
        cases = ENGINE_CASES
        submitted = []
        for sample, options, exact in cases:
            content = (SAMPLES / sample).read_bytes()
            name = f"attack-pack-{options['engine']}-{sample}"
            try:
                plan = self.plan([(name, len(content))], options)
                r = self.upload(content, name, plan=plan, options=options)
            except Exception as error:  # noqa: BLE001
                self.record(g, f"{sample} by {options['engine']}", "FAIL", f"{type(error).__name__}: {error}")
                continue
            if r.status_code != 200:
                self.record(g, f"{sample} by {options['engine']}", "FAIL", f"upload {r.status_code} {r.text[:120]}")
                continue
            submitted.append((sample, options, exact, r.json()["id"], time.monotonic()))
        for sample, options, exact, jid, started in submitted:
            label = f"{sample} by {options['engine']}"
            self.guarded(g, label, lambda: self.judge(label, sample, options, exact, jid, started))
        self.check(g, "the app is healthy after the engine runs", self.healthy())

    def judge(self, label, sample, options, exact, jid, started):
        g = "engines"
        job = self.wait_job(jid)
        seconds = round(time.monotonic() - started)
        status = job.get("status")
        trace = [(t.get("engine"), t.get("status")) for t in job.get("trace", [])]
        ai = (job.get("readers") or {}).get("ai", {})
        if status in ("queued", "processing", None):
            return self.record(g, label, "FAIL", f"still {status} after {self.wait}s")
        if status == "error":
            return self.record(g, label, "FAIL", f"error: {job.get('error')} trace {trace}")
        self.check(g, f"{label}: the AI was not called", not ai.get("calls"), f"ai {ai.get('status')} calls {ai.get('calls')}")
        if exact:
            wrong = _compare(job.get("invoice") or {}, json.loads((SAMPLES / sample).with_suffix(".json").read_text()))
            self.check(g, f"{label}: every expected field read exactly", not wrong,
                       f"{seconds}s after upload, {job.get('selected_engine')}; " + ("; ".join(wrong[:6]) if wrong else "all fields match"))
        else:
            mine = [s for e, s in trace if e == options["engine"]]
            self.check(g, f"{label}: the chosen engine read the file", any(s in ("extracted", "text_only") for s in mine),
                       f"{seconds}s after upload, trace {trace}")

    def wait_job(self, jid):
        deadline = time.monotonic() + self.wait
        job = {}
        while time.monotonic() < deadline:
            r = self.call("GET", f"/api/jobs/{jid}", self.admin)
            if r.status_code == 200:
                job = r.json()
                if job.get("status") not in ("queued", "processing"):
                    return job
            time.sleep(3)
        return job

    # ---- disruptive (opt-in) -------------------------------------------------------------------------------------

    def disrupt(self):
        g = "disrupt"
        r = self.call("GET", "/api/state", self.admin)
        settings = r.json().get("settings", {})
        codes = []
        for _ in range(65):
            codes.append(self.call("POST", "/api/settings", self.admin, json=settings).status_code)
            if codes[-1] == 429:
                break
        self.check(g, "admin writes are limited to 60 a minute", codes[-1] == 429 and len(codes) <= 61,
                   f"{len(codes)} writes, last {codes[-1]}; admin writes are blocked for a minute now")
        codes = []
        for _ in range(205):
            codes.append(self.call("POST", "/api/preflight", self.admin, json={"options": LOCAL_ONLY, "files": [{"name": "a.pdf", "size": 1}]}).status_code)
            if codes[-1] == 429:
                break
        self.check(g, "pending plans are capped near 200", codes[-1] == 429, f"{len(codes)} plans, last {codes[-1]}; new uploads wait up to 10 minutes")
        self.check(g, "the app is healthy after the floods", self.healthy())

    # ---- clean-up ------------------------------------------------------------------------------------------------

    def cleanup(self):
        if not self.created or self.keep or not self.admin:
            if self.created:
                self.record("cleanup", f"{len(self.created)} invoices left in place", "SKIP", " ".join(self.created))
            return
        items = []
        for jid in self.created:
            job = self.wait_job(jid)
            if job.get("revision"):
                items.append({"id": jid, "revision": job["revision"]})
        if not items:
            return
        r = self.call("POST", "/api/jobs/delete", self.admin, json={"jobs": items, "confirm_permanent": True})
        self.check("cleanup", f"the {len(items)} invoices this run created are deleted", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

    # ---- run -----------------------------------------------------------------------------------------------------

    def run(self, groups):
        self.config = {}
        plan = [("edge", self.edge, False), ("tokens", self.tokens, False), ("csrf", self.csrf, True),
                ("authz", self.authz, True), ("upload", self.uploads, "admin"), ("plan", self.plans, "admin"),
                ("load", self.load, True), ("engines", self.engines, "admin"), ("disrupt", self.disrupt, "admin")]
        try:
            for name, fn, needs in plan:
                if groups and name not in groups:
                    continue
                if name == "disrupt" and not self.disruptive:
                    self.record(name, "rate limit and plan floods", "SKIP", "pass --disruptive to run them")
                    continue
                if needs == "admin" and not self.admin or needs is True and not (self.admin or self.member):
                    self.record(name, "the whole group", "SKIP", "needs " + ("an admin token" if needs == "admin" else "a token"))
                    continue
                self.guarded(name, "the group stopped", fn)
        finally:
            self.guarded("cleanup", "delete what this run created", self.cleanup)
        return self.results


# ---- hostile files, built here so nothing hostile is committed ---------------------------------------------------

def _pdf(objects):
    """A PDF from object bodies (object 1 is the catalog) with a correct cross-reference table."""
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for o in offsets:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _stream(data, extra=b""):
    return b"<< /Length %d %s >>\nstream\n" % (len(data), extra) + data + b"\nendstream"


def _pdf_pages(count):
    kids = " ".join(f"{3 + n} 0 R" for n in range(count)).encode()
    pages = [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"] * count
    return _pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % count] + pages)


def _pdf_xobject_bomb(depth):
    """Each form draws the next one twice: 2^depth draws on one page."""
    first = 5
    forms = []
    for n in range(depth):
        if n == depth - 1:
            forms.append(_stream(b"0 0 1 1 re f", b"/Type /XObject /Subtype /Form /BBox [0 0 1 1]"))
        else:
            ref = f"/Resources << /XObject << /X {first + n + 1} 0 R >> >>".encode()
            forms.append(_stream(b"/X Do /X Do", b"/Type /XObject /Subtype /Form /BBox [0 0 1 1] " + ref))
    page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /XObject << /X 5 0 R >> >> >>"
    return _pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", page,
                 _stream(b"q 612 0 0 792 0 0 cm /X Do Q")] + forms)


def _pdf_inflate_bomb(size=1_000_000_000):
    compressor = zlib.compressobj(9)
    chunk = b" " * 1_000_000
    data = b"".join(compressor.compress(chunk) for _ in range(size // len(chunk))) + compressor.flush()
    page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>"
    return _pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", page,
                 _stream(data, b"/Filter /FlateDecode")])


def _png(width, height):
    """A grey PNG of all zeros: a few hundred KB however large it claims to be."""
    def chunk(kind, data):
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")
    compressor = zlib.compressobj(9)
    row = b"\0" * (width + 1)
    body = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()
    head = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 0, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", body) + chunk(b"IEND", b"")


def _tiff_frames(count):
    from PIL import Image
    out = io.BytesIO()
    frames = [Image.new("L", (16, 16)) for _ in range(count)]
    frames[0].save(out, format="TIFF", save_all=True, append_images=frames[1:])
    return out.getvalue()


def _zip_bomb(member, size):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        with archive.open(member, "w") as entry:
            chunk = b"0" * 1_000_000
            for _ in range(size // len(chunk)):
                entry.write(chunk)
            entry.write(b"0" * (size % len(chunk)))
    return out.getvalue()


# ---- helpers -----------------------------------------------------------------------------------------------------

def _hsts_seconds(value):
    for part in value.split(";"):
        key, _, number = part.strip().partition("=")
        if key.lower() == "max-age" and number.isdigit():
            return int(number)
    return 0


def _hs256(claims, secret, kid):
    import hashlib
    import hmac
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    signing = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode()) + "." + b64(json.dumps(claims).encode())
    return signing + "." + b64(hmac.new(secret, signing.encode(), hashlib.sha256).digest())


def _tenant_kids(tenant):
    try:
        keys = httpx.get(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", timeout=10).json()["keys"]
        return [k["kid"] for k in keys if k.get("kid")]
    except Exception:  # noqa: BLE001 - no network or no tenant: a made-up kid is used instead
        return []


def _az_token(resource):
    if not shutil.which("az"):
        return None
    try:
        out = subprocess.run(["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken",
                              "-o", "tsv"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def _compare(invoice, expected):
    """The golden-sample comparison (tests/test_golden_samples.py): text exact, amounts as numbers."""
    def same(field, mine, theirs):
        if field in AMOUNTS and mine is not None and theirs is not None:
            try:
                return Decimal(str(mine)) == Decimal(str(theirs))
            except InvalidOperation:
                return False
        return mine == theirs
    wrong = [f"{f}={invoice.get(f)!r} want {v!r}" for f, v in expected.items() if f != "lines" and not same(f, invoice.get(f), v)]
    lines = invoice.get("lines") or []
    if len(lines) != len(expected.get("lines", [])):
        wrong.append(f"{len(lines)} lines, want {len(expected.get('lines', []))}")
    for n, (line, want) in enumerate(zip(lines, expected.get("lines", [])), 1):
        wrong += [f"line {n} {f}={line.get(f)!r} want {v!r}" for f, v in want.items() if not same(f, line.get(f), v)]
    return wrong


def _token(env, prompt):
    value = os.getenv(env, "").strip()
    if not value and sys.stdin.isatty():
        value = getpass.getpass(prompt).strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value or None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True, help="the deployed app, https://...")
    parser.add_argument("--only", default="", help="comma-separated groups: edge,tokens,csrf,authz,upload,plan,load,engines,disrupt")
    parser.add_argument("--disruptive", action="store_true", help="also run the floods that lock admin writes for a minute and new plans for up to 10")
    parser.add_argument("--keep", action="store_true", help="leave the invoices this run created")
    parser.add_argument("--wait", type=int, default=900, help="seconds to wait for each invoice to be read (default 900)")
    parser.add_argument("--report", default="", help="also write the results as JSON to this file")
    args = parser.parse_args(argv)
    if not args.url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        parser.error("--url must be https:// (plain http only for a local run)")
    admin = _token("INV_ATTACK_ADMIN_TOKEN", "Admin access token (blank to skip): ")
    member = _token("INV_ATTACK_MEMBER_TOKEN", "Member access token (blank to skip): ")
    groups = {g.strip() for g in args.only.split(",") if g.strip()}
    pack = Pack(args.url, admin, member, disruptive=args.disruptive, keep=args.keep, wait=args.wait)
    started = time.monotonic()
    results = pack.run(groups)
    counts = {s: sum(r["status"] == s for r in results) for s in ("PASS", "FAIL", "WARN", "SKIP")}
    print(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['WARN']} warnings, {counts['SKIP']} skipped "
          f"in {round(time.monotonic() - started)}s against {args.url}")
    if args.report:
        Path(args.report).write_text(json.dumps({"url": args.url, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                 "counts": counts, "results": results}, indent=1))
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
