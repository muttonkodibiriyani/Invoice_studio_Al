# Aggressive test pack for the deployed app

`scripts/attack_pack.py` attacks a deployed Invoice Studio over HTTPS and prints one line per case: **PASS**,
**FAIL**, **WARN** or **SKIP**, with what came back. It exits 0 when no case failed and 1 when one did. A FAIL is a
finding: it starts another round of the improvement loop.

It uses only the deployed app. The AI is never asked (every upload runs with AI fallback and cross-check off), no
setting is changed, and every invoice the run creates is deleted at the end.

## Run it

From a checkout with the app's environment (`./scripts/setup.sh` once):

```bash
# 1. Tokens, from the browser (as in docs/AZURE.md §5): open the app, Sign in with Microsoft, developer tools →
#    Network, click any /api/ request, copy the Authorization header after "Bearer ".
#    One account in the admin group; optionally a second in the member group.
#    Paste at the hidden prompts, so they stay out of the shell history.
.venv/bin/python scripts/attack_pack.py --url https://<app>.<region>.azurecontainerapps.io --report pack.json
```

The tokens can also come from `INV_ATTACK_ADMIN_TOKEN` and `INV_ATTACK_MEMBER_TOKEN`. A blank prompt skips that
token: with no token at all only the signed-out groups run (`edge`, `tokens`); with no member token the
member-against-admin sweep is skipped. Entra tokens last about an hour; the full run takes 10 to 20 minutes.

| Option | What it does |
|---|---|
| `--only edge,tokens,...` | Run only these groups. |
| `--disruptive` | Also run the floods (below). Run them out of hours. |
| `--keep` | Leave the invoices the run created (their ids are printed). |
| `--wait 900` | Seconds to wait for each invoice to be read. |
| `--report pack.json` | Also write every result as JSON. Attach it to the task. |

## What it attacks, and what a pass is

| Group | Cases | Pass |
|---|---|---|
| `edge` | Health; public config; security headers on `/`, a public route, a 401 and a 404; `/docs`, `/redoc`, `/openapi.json`; path traversal into `/static`, `/.env`, `/.git/config`; a foreign `Host` header; plain HTTP; TLS 1.0 and 1.1. | Public config names the tenant and app and holds no secret. Every response carries `nosniff`, `no-referrer`, `no-store`, a CSP with `frame-ancestors 'none'`, `object-src 'none'` and no `unsafe` script source, HSTS of a year, and a request id. No schema page and no file outside `/static`. Foreign host refused. HTTP redirects to HTTPS or is refused. Old TLS refused. A version banner is a WARN. |
| `tokens` | All 70 signed-in API routes with no token. Tokens naming this tenant and app with the admin role but not signed by Entra: own RSA key with the tenant's real key id, unknown key id, `alg: none`, HS256 keyed with a public key, the v1 issuer, garbage. Your real token with its payload edited, its roles edited, its signature cut. Basic auth, an empty Bearer, lower-case `bearer`, a 20 KB token. A real Entra token for Microsoft Graph (needs `az login`; SKIP without). | Every one answers 401 (a 20 KB token may be 400 or 431), and no answer leaks a trace. |
| `csrf` | A write without `X-Studio-Request`; from a foreign `Origin`; from `Origin: null`; a CORS preflight from a foreign origin. | 403 on each write; no CORS grant. |
| `authz` | Your token's role. A member token on all 27 admin routes. Made-up, traversal and SQL-shaped invoice ids. SQL-shaped and 5000-character lookup searches. Secrets in `/api/state`. An extra `is_admin` field on settings. Deleting without the confirm flag, or with `"true"` as a string. | Admin token is admin, member token is member. Every admin route refuses the member with 403. Unknown ids are 404. No 5xx and no leak. No key in state. 422 on the extra field and the string flag, 400 without the flag. |
| `upload` | `.exe`, `.html`, `.svg` with script, HTML named `.pdf`, an empty file, a PDF header only, a broken encrypted PDF, 21 pages, a page that draws itself 2^40 times, a 1 GB inflate bomb, a 36 and a 400 megapixel PNG, a broken image, a 25-frame TIFF, a DOCX that expands past 30 MB, an XLSX with no workbook, a non-UTF-8 CSV, a 12.3 MB upload. Then a real PDF named `../../../../etc/attack-pack-<img src=x onerror=alert(1)>.pdf`. | Each hostile file is refused (400, or 409 once it parsed but has no plan; 413 for size) in under 45 s, with no leak, and the app stays healthy. Refused but slower than 45 s is a WARN. The odd name is stored as its base name only, and the stored file comes back as `application/pdf` with `nosniff` and the CSP. |
| `plan` | An unconfirmed plan; a size, name or option that differs from the confirmed plan; a made-up plan token; one plan file uploaded twice; a member using the admin's plan. Preflight with 501 files, a file over 12 MB or of 0 bytes, an unknown engine or provider, a model name starting with `-`, an extra field, a 301-character name, a size of `1e309`, broken JSON, a 10 MB JSON array, JSON nested 100 000 deep. | 409 for every plan mismatch, 422 (or 400) for every bad preflight, no leak, healthy after. The member-uses-admin's-plan case is a WARN (see findings). |
| `load` | 200 reads of `/api/state`, 20 at a time. | No 5xx; health answers in the tail. p95 over 10 s is a WARN. |
| `engines` | invoice2data, PaddleOCR and Docling installed in the image. The five golden samples read by the whole local chain (`auto`). Each engine forced alone: invoice2data on `invoice.pdf`, PaddleOCR and Docling on `invoice-scan.png`, Docling on `invoice.pdf` with native text off. | Each engine installed. Each golden sample read to exactly `samples/<name>.json` (text exact, amounts as numbers, the rule of `tests/test_golden_samples.py`), with no AI call. Each forced engine's own trace says `extracted` or `text_only`. An invoice in `error`, or still reading after `--wait`, fails. Times are since upload, so they include the queue. |
| `disrupt` (opt-in) | 65 admin writes in a row (settings saved back unchanged); 205 processing plans. | The 61st write or earlier is 429; plans stop near 200 with 429; healthy after. **Side effects:** admin writes are blocked for a minute, and new uploads for everyone wait up to 10 minutes until the plans expire. |
| `cleanup` | Deletes every invoice the run created. | 200. If it fails, delete the printed ids on the Invoices page. |

## What a failure means

- **edge, tokens, csrf, authz:** a security control is missing on the deployed app. Stop the go-live and open a task
  with the line and the `X-Request-Id` from the app log.
- **upload, plan:** a hostile file got further than it should, or the app errored. The request id is in the 500
  answer; the app log has the trace.
- **engines:** a reader is missing from the image, or reads a golden sample differently in the container than on
  the build machine. That is the next round of the improvement loop.

## Proven against the app

`tests/test_attack_pack.py` keeps the pack honest. The route list must equal the app's own signed-in routes and
their levels, so a new route cannot miss the signed-out sweep. Then the pack runs against the app in cloud mode
with a stand-in Entra tenant, where the current app must pass every fast group and leave no invoice behind. It
also runs against apps broken on purpose, and must FAIL each: a security header removed, any token accepted, a
member allowed on admin routes, the request-protection header no longer required. An engine invoice that errored,
stalled, called the AI, or whose forced reader did not read must also fail.

The whole pack, the slow groups and `--disruptive` included, was also run over real HTTP against the app in
cloud mode, with all three readers installed (31 Oct 2026): no FAIL; the warnings are the two findings below.

## Findings from building the pack

| | Sev | Finding | Where |
|---|---|---|---|
| F1 | M | A 1 KB PDF whose page draws itself 2^40 times holds its upload request for the full 240 s reader limit before it is refused. The multi-invoice check runs per request in its own child with no cap on how many run at once, and the upload route waits for it on the request threads. A signed-in member sending a few dozen such files at once can hold every request thread and burn every CPU for four minutes: the app stops answering everyone. | `app/main.py` `submit` → `engines.split_check`; limit `INV_ENGINE_TIMEOUT` 240 s |
| F2 | L | A confirmed processing plan is not tied to the person who made it: anyone signed in who has the plan token can upload under it. The token is a 256-bit secret that only its maker's browser sees, so this is defence in depth. | `app/main.py` `require_plan` |

## What the pack does not do

- It does not change AI connections, settings, rules, references or templates, and does not try SSRF through the
  AI endpoint setting: on production those writes would change what real invoices go through.
- It does not flood the processing queue or the request threads (F1): that would take the app down for everyone.
- It does not check the UI. Open the invoice named `attack-pack-<img ...>.pdf` with `--keep` and confirm the name
  shows as text, with no picture box and no alert.
- It does not read the app's log. After a run, check that the `ip` in the request log is your public address
  (docs/AZURE.md §5, "the address the app sees"): the pack cannot see that from outside.
