"""Build the API documentation from the app's own route table and scripts/api_catalogue.py.

    .venv/bin/python scripts/build_api_docs.py           # writes docs/API.md and docs/api/*
    .venv/bin/python scripts/build_api_docs.py --check   # exits 1 if a written file is out of date

Outputs:
  docs/API.md                                   the reference, in Markdown
  docs/api/Invoice_Studio_API.pdf               the same reference as a PDF, for InfoSec and testers
  docs/api/invoice-studio.postman_collection.json   Postman / newman: smoke, security checks, invoice flow, reference
  docs/api/invoice-studio.postman_environment.json  baseUrl and tokens to fill in

Access levels come from the app (`@access(...)` on each route), never from this script, so a level in the docs is
the level the server enforces.
"""
import argparse
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from api_catalogue import AREAS, LOCAL_ONLY, NOT_API, ROUTES  # noqa: E402

MARKDOWN = ROOT / "docs" / "API.md"
OUT = ROOT / "docs" / "api"
PDF = OUT / "Invoice_Studio_API.pdf"
COLLECTION = OUT / "invoice-studio.postman_collection.json"
ENVIRONMENT = OUT / "invoice-studio.postman_environment.json"
FRAMEWORK = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
SAMPLE = ROOT / "samples" / "invoice.pdf"
PATH_VARS = {"jid": "jobId", "eid": "exportId", "bid": "batchId", "tid": "taskId", "fid": "feedbackId",
             "key": "learnedKey", "version": "masterVersion", "upload_id": "mappingUploadId",
             "version_id": "mappingVersionId", "aid": "accountId"}
PATH_FIXED = {"provider": "openai", "kind": "export-batch"}
LEVEL_TEXT = {"public": "Public", "member": "Member", "admin": "Admin", "none": "No token"}


def app_routes():
    """(method, path, level) for every route the app serves, from the app itself."""
    os.environ.setdefault("INV_STUDIO_LEARN", "0")
    from app.authorization import ATTRIBUTE
    from app.main import create_app
    app = create_app(Path(tempfile.mkdtemp(prefix="api-docs-")))
    found = []
    for route in app.router.routes:
        path, endpoint = getattr(route, "path", ""), getattr(route, "endpoint", None)
        if endpoint is None or path in FRAMEWORK or path.startswith("/static"):
            continue
        for method in sorted(getattr(route, "methods", None) or []):
            if method == "HEAD":
                continue
            found.append((method, path, getattr(endpoint, ATTRIBUTE, None) or "none"))
    return found


def documented(routes=None):
    """The catalogue entries joined with the app's levels; refuses any route missing on either side."""
    routes = routes if routes is not None else app_routes()
    served = {(m, p): level for m, p, level in routes if (m, p) not in NOT_API}
    missing = sorted(set(served) - set(ROUTES))
    gone = sorted(set(ROUTES) - set(served))
    if missing or gone:
        raise SystemExit(f"api_catalogue.py is out of date. Not documented: {missing}. No longer served: {gone}")
    entries = []
    for (method, path), info in ROUTES.items():
        entries.append({"method": method, "path": path, "level": served[(method, path)], **info})
    return entries


# ---------------------------------------------------------------- Markdown

GUIDE = """# Invoice Studio API

Every route the deployed app serves, with how to call it and what it needs. Generated from the app's own route
table by `scripts/build_api_docs.py`; the access level shown is the one the server enforces. The same content is in
`docs/api/Invoice_Studio_API.pdf`, and `docs/api/invoice-studio.postman_collection.json` runs it.

## 1. Base URL and access levels

`https://<app>.<region>.azurecontainerapps.io` (the `appUrl` output of the deploy), or the custom host name.
HTTPS only: plain HTTP is redirected. Swagger, ReDoc and `/openapi.json` are switched off in the cloud build.

| Level | Who | How |
|---|---|---|
| No token | anyone | `/health` and the page itself; no data |
| Public | anyone | `GET /api/public-config` only: sign-in settings, no secret, no data |
| Member | users with the `member` or `admin` app role | Entra ID bearer token |
| Admin | users with the `admin` app role | Entra ID bearer token; 60 admin writes per minute per user |

The role is read from the token on every request. The app's user table records sign-ins and grants nothing:
roles are given by assigning people (or their group) to the app role in Entra ID.

## 2. Getting a token

The API accepts only Entra ID v2 access tokens that the Invoice Studio app registration itself requested: RS256,
issuer `https://login.microsoftonline.com/<tenant>/v2.0`, audience the app's client id, scope `access_as_user`,
`azp` the app's client id, not expired. A token for any other client (Azure CLI, Graph Explorer, Postman's own
client) is refused with 401 by design, so the token comes from the app's sign-in in the browser:

1. Open the app and **Sign in with Microsoft**.
2. Open the browser developer tools (F12) → **Network**, and click any request to `/api/` (for example `state`).
3. Under **Request Headers**, copy the value of `Authorization` after `Bearer ` (it starts with `eyJ`).
4. Paste it into the Postman environment's `token` (and a member's token into `memberToken`).

A token lasts about an hour. Never paste a token into a chat, a ticket or a commit: it is the user's access.

## 3. Headers

| Header | When | Value |
|---|---|---|
| `Authorization` | every Member and Admin call | `Bearer <token>` |
| `X-Studio-Request` | every POST, PUT, PATCH and DELETE | `1`. A write without it is refused with 403 (request forgery protection). |
| `Origin` | sent by browsers | must be the app's own address. Postman and curl send none, which is accepted. |
| `Content-Type` | JSON bodies | `application/json`; uploads are `multipart/form-data` |

Every response carries `X-Request-Id`; quote it when reporting a problem, it is in the app log. Responses also carry
HSTS (one year), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` and a
Content-Security-Policy with `frame-ancestors 'none'`.

## 4. Answers and errors

| Status | Meaning |
|---|---|
| 200 | Done. JSON, or a file for downloads. |
| 400 | The request is wrong; `detail` says why in plain words. |
| 401 | No token, an expired token, or a token not issued to this app. |
| 403 | Signed in but not allowed: member on an admin route, write without `X-Studio-Request`, foreign `Origin`, AI turned off, or an AI provider this deployment does not allow. |
| 404 | No such route or record. An unknown `/api/` path is always 404. |
| 409 | The state changed: plan not confirmed or not yours, stale revision, invoice still reading or already exported. |
| 413 | Upload over 12 MB. |
| 422 | The body does not match the schema (unknown or missing field, wrong type). |
| 429 | Rate limit: admin writes, too many plans, or the reading queue is full. Wait and retry. |
| 500, 503 | Server side. The body has the request id, never a trace. `/api/health/dependencies` answers 503 when the database or storage cannot be reached. |

Limits: 12 MB per file, 500 files per plan, a confirmed plan stays valid 10 minutes after its last use.

## 5. Testing a deployment

**Postman.** Import both files from `docs/api/`, choose the *Invoice Studio* environment, set `baseUrl` and `token`
(and `memberToken` if you have a member account), then run the folders in order:

| Folder | What it proves | Changes data? |
|---|---|---|
| 1. Smoke test | The app, sign-in, database and storage answer; security headers are present | No |
| 2. Security checks | No token, forged token, missing request header, foreign origin, unknown route, schema pages, member on admin route, non-Azure AI provider: each refused | No (each write is refused) |
| 3. Invoice flow | Plan, confirm, upload the synthetic sample, read it locally (no AI), audit trail, then delete it | Creates and deletes one test invoice (needs an admin token) |
| 4. API reference | Every route with an example; for reading, not for running as a whole | Yes, if run |

For the upload in folder 3, Postman needs `samples/invoice.pdf` from this repository: set Postman's working
directory to the checkout, or select the file on the request.

**From Azure Cloud Shell, without Postman** (Cloud Shell has Node.js):

```bash
cd ~/Invoice_studio_Al
read -rs TOKEN && export TOKEN          # paste the token, Enter (stays out of the history)
npx --yes newman@6 run docs/api/invoice-studio.postman_collection.json \\
  -e docs/api/invoice-studio.postman_environment.json --working-dir . \\
  --env-var baseUrl=https://<app>.<region>.azurecontainerapps.io --env-var token="$TOKEN" \\
  --folder "1. Smoke test" --folder "2. Security checks" --folder "3. Invoice flow"
```

Every check prints a tick or a cross, and newman exits non-zero if any failed. For the full attack pack (70+ routes
without a token, forged tokens, hostile files, floods) see `docs/ATTACK_PACK.md`.

## 6. Routes
"""


def example_url(entry, *, postman=False):
    path = entry["path"]
    for name, var in PATH_VARS.items():
        path = path.replace("{" + name + "}", "{{" + var + "}}" if postman else f"<{var}>")
    for name, value in PATH_FIXED.items():
        path = path.replace("{" + name + "}", value)
    return path


def markdown(entries):
    lines = [GUIDE]
    for area, title, intro in AREAS:
        rows = [e for e in entries if e["area"] == area]
        if not rows:
            continue
        lines += [f"### {title}", "", intro, "", "| Method | Path | Access | What it does |", "|---|---|---|---|"]
        for e in rows:
            text = e["text"].replace("|", "\\|")
            lines.append(f"| `{e['method']}` | `{e['path']}` | {LEVEL_TEXT[e['level']]} | **{e['title']}.** {text} |")
        lines.append("")
        for e in rows:
            if e.get("body") is not None or e.get("form") or e.get("query"):
                lines.append(f"`{e['method']} {e['path']}`")
                if e.get("query"):
                    lines.append("query: `" + "&".join(f"{k}={v}" for k, v in e["query"].items()) + "`")
                if e.get("body") is not None:
                    lines += ["```json", json.dumps(e["body"], indent=2), "```"]
                if e.get("form"):
                    lines.append("multipart form: " + ", ".join(
                        f"`{k}` = " + (f"file `{v[1:]}`" if isinstance(v, str) and v.startswith("@") else
                                       f"`{json.dumps(v) if not isinstance(v, str) else v}`")
                        for k, v in e["form"].items()))
                lines.append("")
    total = len(entries)
    counts = {lvl: sum(e["level"] == lvl for e in entries) for lvl in ("public", "member", "admin", "none")}
    lines.append(f"Totals: {total} routes: {counts['public']} public, {counts['member']} member, "
                 f"{counts['admin']} admin, {counts['none']} without a token (health).")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- Postman

STATUS_TEST = "pm.test('status is {code}', function () {{ pm.response.to.have.status({code}); }});"
NO_LEAK = ("pm.test('no stack trace or secret in the answer', function () { "
           "pm.expect(pm.response.text()).to.not.match(/Traceback|File \\\"\\/|password|api[-_]key\\\"\\s*:\\s*\\\"[^\\\"]/i); });")
HEADERS = ("pm.test('security headers', function () { "
           "pm.expect(pm.response.headers.get('X-Content-Type-Options')).to.eql('nosniff'); "
           "pm.expect(pm.response.headers.get('Cache-Control') || '').to.include('no-store'); "
           "pm.expect(pm.response.headers.get('Content-Security-Policy') || '').to.include(\"frame-ancestors 'none'\"); "
           "pm.expect(pm.response.headers.get('Strict-Transport-Security') || '').to.include('max-age='); "
           "pm.expect(pm.response.headers.get('X-Request-Id') || '').to.not.eql(''); });")


def url(path, query=None):
    raw = "{{baseUrl}}" + path + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")
    item = {"raw": raw, "host": ["{{baseUrl}}"], "path": [p for p in path.split("/") if p]}
    if query:
        item["query"] = [{"key": k, "value": str(v)} for k, v in query.items()]
    return item


def request(name, method, path, *, body=None, form=None, query=None, tests=(), pre=(), auth=None, headers=None,
            description=""):
    head = [] if method == "GET" else [{"key": "X-Studio-Request", "value": "1"}]
    if headers is not None:
        head = headers
    req = {"method": method, "header": head, "url": url(path, query), "description": description}
    if body is not None:
        req["header"] = head + [{"key": "Content-Type", "value": "application/json"}]
        raw = json.dumps(body, indent=2).replace(json.dumps(REVISION), "{{jobRevision}}")
        req["body"] = {"mode": "raw", "raw": raw, "options": {"raw": {"language": "json"}}}
    if form:
        fields = []
        for key, value in form.items():
            if isinstance(value, str) and value.startswith("@"):
                fields.append({"key": key, "type": "file", "src": value[1:]})
            else:
                fields.append({"key": key, "type": "text", "value": value if isinstance(value, str) else json.dumps(value)})
        req["body"] = {"mode": "formdata", "formdata": fields}
    if auth is not None:
        req["auth"] = auth
    events = []
    if pre:
        events.append({"listen": "prerequest", "script": {"type": "text/javascript", "exec": list(pre)}})
    if tests:
        events.append({"listen": "test", "script": {"type": "text/javascript", "exec": list(tests)}})
    item = {"name": name, "request": req}
    if events:
        item["event"] = events
    return item


NOAUTH = {"type": "noauth"}
REVISION = "__JOB_REVISION__"  # written into the raw body as the number {{jobRevision}}, not a string
MEMBER_AUTH = {"type": "bearer", "bearer": [{"key": "token", "value": "{{memberToken}}", "type": "string"}]}
SKIP_WITHOUT_MEMBER = "if (!pm.environment.get('memberToken')) { pm.execution.skipRequest(); }"


def smoke_folder(entries):
    items = []
    for e in entries:
        if e.get("smoke") is None:
            continue
        tests = [STATUS_TEST.format(code=e["smoke"]), NO_LEAK]
        if e["path"] == "/api/public-config":
            tests.append("pm.test('names the tenant and app, holds no secret', function () { var j = pm.response.json(); "
                         "pm.expect(JSON.stringify(j)).to.not.match(/secret|password/i); });")
        if e["path"] in ("/api/session", "/api/state", "/api/health/dependencies"):
            tests.append(HEADERS)
        if e["path"] == "/api/session":
            tests.append("pm.test('the token carries an app role', function () { "
                         "pm.expect(['admin','member']).to.include(pm.response.json().role); });")
        if e["path"] == "/api/health/dependencies":
            tests.append("pm.test('database and storage answer', function () { var j = pm.response.json(); "
                         "pm.expect(j.database).to.eql('ok'); pm.expect(j.storage).to.eql('ok'); });")
        auth = NOAUTH if e["level"] in ("none", "public") else None
        items.append(request(f"{e['title']} ({e['method']} {e['path']})", e["method"], e["path"], query=e.get("query"),
                             tests=tests, auth=auth, description=e["text"]))
    return {"name": "1. Smoke test", "description": "Read-only. Every request must answer as listed after a deploy. "
            "Admin routes need an admin token.", "item": items}


def security_folder():
    # Built when the request runs, so no token-shaped string sits in the file (the secret scanner would flag it).
    forge = ("var btoa = require('btoa'); function b64(o) { return btoa(JSON.stringify(o)).replace(/=+$/, '').replace(/\\+/g, '-')"
             ".replace(/\\//g, '_'); } pm.variables.set('forgedToken', b64({alg: 'RS256', kid: 'not-a-real-key', "
             "typ: 'JWT'}) + '.' + b64({aud: 'x', roles: ['admin'], exp: 4102444800}) + '.c2lnbmF0dXJl');")
    body = {"options": LOCAL_ONLY, "files": [{"name": "invoice.pdf", "size": 2140}]}
    return {"name": "2. Security checks", "description": "Each request is an attack the app must refuse. None changes data.",
            "item": [
                request("No token is refused (401)", "GET", "/api/state", auth=NOAUTH,
                        tests=[STATUS_TEST.format(code=401), NO_LEAK]),
                request("A forged token is refused (401)", "GET", "/api/state", auth=NOAUTH,
                        headers=[{"key": "Authorization", "value": "Bearer {{forgedToken}}"}], pre=[forge],
                        tests=[STATUS_TEST.format(code=401), NO_LEAK]),
                request("A write without X-Studio-Request is refused (403)", "POST", "/api/preflight", body=body,
                        headers=[], tests=[STATUS_TEST.format(code=403), NO_LEAK]),
                request("A write from a foreign Origin is refused (403)", "POST", "/api/preflight", body=body,
                        headers=[{"key": "X-Studio-Request", "value": "1"}, {"key": "Origin", "value": "https://evil.example"}],
                        tests=[STATUS_TEST.format(code=403), NO_LEAK]),
                request("An unknown API route is 404", "GET", "/api/does-not-exist",
                        tests=[STATUS_TEST.format(code=404), NO_LEAK]),
                request("Swagger is off (404)", "GET", "/docs", auth=NOAUTH, tests=[STATUS_TEST.format(code=404)]),
                request("The OpenAPI schema is off (404)", "GET", "/openapi.json", auth=NOAUTH,
                        tests=[STATUS_TEST.format(code=404)]),
                request("A member cannot use an admin route (403)", "GET", "/api/users", auth=MEMBER_AUTH,
                        pre=[SKIP_WITHOUT_MEMBER], tests=[STATUS_TEST.format(code=403), NO_LEAK]),
                request("A member cannot delete invoices (403)", "POST", "/api/jobs/delete", auth=MEMBER_AUTH,
                        body={"jobs": [{"id": "x", "revision": 1}], "confirm_permanent": True}, pre=[SKIP_WITHOUT_MEMBER],
                        tests=[STATUS_TEST.format(code=403), NO_LEAK]),
                request("A non-Azure AI provider is refused (403)", "POST", "/api/connections/openai",
                        body={"api_key": "sk-not-a-real-key-0000000000"}, tests=[STATUS_TEST.format(code=403), NO_LEAK],
                        description="InfoSec LLD §9: the Azure deployment allows Azure AI Foundry only."),
                request("Deleting without the confirm flag is refused (400)", "POST", "/api/jobs/delete",
                        body={"jobs": [{"id": "not-a-real-invoice", "revision": 1}]},
                        tests=[STATUS_TEST.format(code=400), NO_LEAK]),
                request("A made-up plan token is refused (409)", "POST", "/api/preflight/renew",
                        body={"token": "made-up-plan-token"}, tests=[STATUS_TEST.format(code=409), NO_LEAK]),
            ]}


def flow_folder():
    size = SAMPLE.stat().st_size
    plan = {"options": LOCAL_ONLY, "files": [{"name": "invoice.pdf", "size": size}]}
    wait = [
        STATUS_TEST.format(code=200),
        "var j = pm.response.json(); var tries = Number(pm.environment.get('waitTries') || 0);",
        "if ((j.status === 'queued' || j.status === 'processing') && tries < 60) {",
        "  pm.environment.set('waitTries', tries + 1);",
        "  setTimeout(function () {}, 10000);",
        "  pm.execution.setNextRequest(pm.info.requestName);",
        "} else {",
        "  pm.environment.unset('waitTries');",
        "  pm.environment.set('jobRevision', j.revision);",
        "  pm.test('the invoice was read (not error, not still reading)', function () {",
        "    pm.expect(['queued','processing','error']).to.not.include(j.status); });",
        "  pm.test('read without AI', function () {",
        "    pm.expect(JSON.stringify(j.trace || [])).to.not.match(/vision_ai/); });",
        "}",
    ]
    return {"name": "3. Invoice flow", "description": "Plan, confirm, upload the synthetic sample invoice, read it "
            "with the local readers only, check the audit trail, then delete it. Needs an admin token (the delete).",
            "item": [
                request("Preview the plan", "POST", "/api/preflight", body=plan, tests=[
                    STATUS_TEST.format(code=200), "pm.environment.set('planToken', pm.response.json().token);"]),
                request("Confirm the plan", "POST", "/api/preflight/confirm", body={"token": "{{planToken}}"},
                        tests=[STATUS_TEST.format(code=200)]),
                request("Upload the sample invoice", "POST", "/api/invoices",
                        form={"file": "@samples/invoice.pdf", "options": LOCAL_ONLY, "preflight_token": "{{planToken}}"},
                        tests=[STATUS_TEST.format(code=200), NO_LEAK,
                               "pm.environment.set('jobId', pm.response.json().id);"]),
                request("Wait until it is read", "GET", "/api/jobs/{{jobId}}", tests=wait),
                request("Its audit trail", "GET", "/api/jobs/{{jobId}}/audit", tests=[STATUS_TEST.format(code=200)]),
                request("Its source document", "GET", "/api/jobs/{{jobId}}/document", tests=[
                    STATUS_TEST.format(code=200),
                    "pm.test('served as a PDF with nosniff', function () { "
                    "pm.expect(pm.response.headers.get('Content-Type')).to.include('application/pdf'); "
                    "pm.expect(pm.response.headers.get('X-Content-Type-Options')).to.eql('nosniff'); });"]),
                request("Another account cannot use this plan (409)", "POST", "/api/preflight/renew", auth=MEMBER_AUTH,
                        body={"token": "{{planToken}}"}, pre=[SKIP_WITHOUT_MEMBER], tests=[STATUS_TEST.format(code=409)]),
                request("Delete the test invoice", "POST", "/api/jobs/delete",
                        body={"jobs": [{"id": "{{jobId}}", "revision": REVISION}], "confirm_permanent": True},
                        tests=[STATUS_TEST.format(code=200)]),
                request("It is gone (404)", "GET", "/api/jobs/{{jobId}}", tests=[STATUS_TEST.format(code=404)]),
            ]}


def reference_folder(entries):
    folders = []
    for area, title, intro in AREAS:
        items = []
        for e in entries:
            if e["area"] != area:
                continue
            auth = NOAUTH if e["level"] in ("none", "public") else None
            items.append(request(f"{e['title']}", e["method"], example_url(e, postman=True), body=e.get("body"),
                                 form=e.get("form"), query=e.get("query"), auth=auth,
                                 description=f"**{LEVEL_TEXT[e['level']]}.** {e['text']}"))
        if items:
            folders.append({"name": title, "description": intro, "item": items})
    return {"name": "4. API reference", "description": "Every route with an example. For reading and one-off calls; "
            "running the whole folder changes data.", "item": folders}


def collection(entries):
    return {
        "info": {"_postman_id": "6b0f6c1e-5d1a-4c55-9a39-1c0f1a1e2b01", "name": "Invoice Studio API",
                 "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
                 "description": "Generated by scripts/build_api_docs.py from the app's route table. Set baseUrl and "
                                "token in the environment (docs/API.md §2 shows how to get a token)."},
        "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}]},
        "item": [smoke_folder(entries), security_folder(), flow_folder(), reference_folder(entries)],
    }


def environment():
    values = [("baseUrl", "https://<app>.<region>.azurecontainerapps.io", "default"),
              ("token", "", "secret"), ("memberToken", "", "secret")]
    values += [(v, "", "default") for v in ("planToken", "jobId", "jobRevision",
                                            *sorted(set(PATH_VARS.values()) - {"jobId"}))]
    return {"id": "0c8a7a52-4f0e-4b8e-8d0e-6f7e3d1b9a11", "name": "Invoice Studio",
            "values": [{"key": k, "value": v, "type": t, "enabled": True} for k, v, t in values],
            "_postman_variable_scope": "environment"}


# ---------------------------------------------------------------- PDF

def _latin(text):
    """Helvetica has no glyph for these; the PDF stays within Latin-1."""
    for a, b in (("→", "->"), ("§", "Sec. "), ("✅", ""), ("⚠️", ""), ("…", "..."), ("’", "'"), ("“", '"'), ("”", '"')):
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1")


def _inline(text):
    text = _latin(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"<font face='Courier'>\1</font>", text)
    return text


def pdf(entries):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontName="Helvetica", fontSize=9, leading=12)
    small = ParagraphStyle("small", parent=body, fontSize=7.6, leading=9.6, alignment=TA_LEFT)
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=18,
                        textColor=colors.HexColor("#0b3d6e"), spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=12.5,
                        textColor=colors.HexColor("#0b3d6e"), spaceBefore=10, spaceAfter=4)
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], fontName="Helvetica-Bold", fontSize=10.5, spaceBefore=8,
                        spaceAfter=3)
    code = ParagraphStyle("code", parent=body, fontName="Courier", fontSize=7.4, leading=9,
                          backColor=colors.HexColor("#f3f5f8"), borderPadding=4)
    level_colour = {"Public": "#2e7d32", "Member": "#1565c0", "Admin": "#b71c1c", "No token": "#555555"}

    def table(rows, widths, header=True):
        data = [[Paragraph(_inline(c), small) for c in row] for row in rows]
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        style = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c9d1db")),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
        if header:
            style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e3eaf3"))]
        t.setStyle(TableStyle(style))
        return t

    story = []
    blocks = GUIDE.split("\n## ")
    story.append(Paragraph("Invoice Studio API", h1))
    story.append(Paragraph(_inline("Route reference, authentication and test guide for the Azure deployment. Generated "
                                   "from the app's own route table: the access level shown is the one the server "
                                   "enforces."), body))
    story.append(Spacer(1, 4 * mm))
    for block in blocks[1:]:
        title, _, rest = block.partition("\n")
        if title.startswith("6."):
            break
        story.append(Paragraph(_inline(title), h2))
        lines = rest.strip("\n").split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("|"):
                rows = []
                while i < len(lines) and lines[i].startswith("|"):
                    cells = [c.strip() for c in re.split(r"(?<!\\)\|", lines[i].strip())[1:-1]]
                    if not all(set(c) <= {"-", ":"} for c in cells):
                        rows.append([c.replace("\\|", "|") for c in cells])
                    i += 1
                n = len(rows[0])
                widths = {2: [45 * mm, 125 * mm], 3: [35 * mm, 45 * mm, 90 * mm],
                          4: [35 * mm, 55 * mm, 40 * mm, 40 * mm]}.get(n, [170 * mm / n] * n)
                story.append(table(rows, widths))
                story.append(Spacer(1, 2 * mm))
                continue
            if line.startswith("```"):
                block_lines = []
                i += 1
                while i < len(lines) and not lines[i].startswith("```"):
                    block_lines.append(_latin(lines[i]))
                    i += 1
                story.append(Preformatted("\n".join(block_lines), code))
                story.append(Spacer(1, 2 * mm))
                i += 1
                continue
            if line.strip():
                para = [line]
                while (i + 1 < len(lines) and lines[i + 1].strip() and not lines[i + 1].startswith(("|", "```"))
                       and not re.match(r"\d+\. ", lines[i + 1])):
                    i += 1
                    para.append(lines[i])
                text = " ".join(para)
                story.append(Paragraph(_inline(text), body))
                story.append(Spacer(1, 1.5 * mm))
            i += 1
    story.append(PageBreak())
    story.append(Paragraph("Routes", h2))
    for area, title, intro in AREAS:
        rows = [e for e in entries if e["area"] == area]
        if not rows:
            continue
        story.append(Paragraph(_inline(title), h3))
        story.append(Paragraph(_inline(intro), body))
        data = [["Method", "Path", "Access", "What it does"]]
        for e in rows:
            level = LEVEL_TEXT[e["level"]]
            data.append([e["method"], f"`{e['path']}`",
                         f"<font color='{level_colour[level]}'><b>{level}</b></font>", f"**{e['title']}.** {e['text']}"])
        t = Table([[Paragraph(_inline(c) if "<font color" not in c else c, small) for c in row] for row in data],
                  colWidths=[15 * mm, 58 * mm, 17 * mm, 80 * mm], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c9d1db")),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e3eaf3")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
        story.append(t)
        examples = [e for e in rows if e.get("body") is not None or e.get("form") or e.get("query")]
        for e in examples:
            parts = [f"{e['method']} {e['path']}"]
            if e.get("query"):
                parts.append("query: " + "&".join(f"{k}={v}" for k, v in e["query"].items()))
            if e.get("body") is not None:
                parts.append(json.dumps(e["body"], indent=1))
            if e.get("form"):
                parts.append("multipart form:")
                parts += [f"  {k} = " + (v if isinstance(v, str) else json.dumps(v, indent=1).replace("\n", "\n    "))
                          for k, v in e["form"].items()]
            story.append(KeepTogether([Spacer(1, 1.5 * mm), Preformatted(_latin("\n".join(parts)), code)]))
    counts = {lvl: sum(e["level"] == lvl for e in entries) for lvl in ("public", "member", "admin", "none")}
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(_inline(f"Totals: {len(entries)} routes: {counts['public']} public, {counts['member']} "
                                   f"member, {counts['admin']} admin, {counts['none']} without a token."), body))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(20 * mm, 10 * mm, "Invoice Studio API - generated by scripts/build_api_docs.py")
        canvas.drawRightString(190 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title="Invoice Studio API", author="Invoice Studio",
                            subject="API reference", creator="scripts/build_api_docs.py", invariant=1)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def outputs(entries):
    return {
        MARKDOWN: markdown(entries).encode(),
        COLLECTION: (json.dumps(collection(entries), indent=2) + "\n").encode(),
        ENVIRONMENT: (json.dumps(environment(), indent=2) + "\n").encode(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if a written text file is out of date")
    args = parser.parse_args(argv)
    entries = documented()
    files = outputs(entries)
    if args.check:
        stale = [str(p.relative_to(ROOT)) for p, data in files.items() if not p.exists() or p.read_bytes() != data]
        if stale:
            print("Out of date, run scripts/build_api_docs.py: " + ", ".join(stale), file=sys.stderr)
            return 1
        print("API docs are up to date.")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    files[PDF] = pdf(entries)
    for path, data in files.items():
        path.write_bytes(data)
        print(f"wrote {path.relative_to(ROOT)} ({len(data):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
