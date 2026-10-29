# Production readiness — scanning engine release

What was checked before the OCR candidate was offered for release, how it was checked, and what is still open.
Everything below was verified on the deployed candidate or in the code it was built from, not assumed. The deploy and
rollback steps themselves live in the runbook; this file does not repeat them.

Candidate under test: revision `inv-studio-api-ocr-c2`, tag `ocr`, no traffic. Preview channel `ocr-candidate`.
Live traffic stayed on `inv-studio-api-00099-7n2` throughout.

## Secrets and configuration

- The only two secrets the service needs — the database URL and the vault key — are mounted from Secret Manager
  (`inv-studio-database-url`, `inv-studio-vault-key`, both `latest`), never baked into the image or the revision.
  `gcloud run services describe` shows them as `secretKeyRef`, so a revision description can be shared safely.
- `INV_STUDIO_FIREBASE_API_KEY` is plain configuration, not a secret: a Firebase web API key is published to every
  browser that loads the app. Access is decided by the identity provider and the user directory, not by that key.
- Logs cannot carry either secret: `app/logging_setup.py::redact` removes bearer tokens, JWT-shaped strings,
  `token=`/`apiKey=`/`secret=`/`oobCode=` parameters and whole URLs from every message before it is written.
- **Open until go-live:** the service template currently holds the candidate's environment —
  `INV_STUDIO_RECOVER_JOBS=0` and the two preview origins in `INV_STUDIO_ALLOWED_ORIGINS`. A revision created from
  the template as it stands would inherit them. Runbook section 1 step 7 (reset the candidate environment) has to run
  before the go-live revision is built.

## Error handling and logging

- One structured JSON line per request (`inv_studio.request`): method, matched route, status, latency, user id,
  caller address, and a request id taken from the Cloud Trace header when the front end sends one. `/health` and
  `/static/**` are logged only when they fail, so the uptime check does not drown the log.
- Failures are mapped deliberately: a bad value is a 400 with the message, invalid input a 422 with the field names
  but never the submitted value, a missing protection header or a foreign origin a 403, an oversize upload a 413.
  An unhandled exception is logged with the route and the user before it leaves the handler.
- A reader that fails does not fail the read: each engine's outcome is recorded in the trace and the chain moves on.
  An AI read that came back cut off or timed out is a named outcome (`INCOMPLETE`, `TIMED_OUT`), retried once whole
  and then read page by page, rather than a silent partial invoice.
- Where a value cannot be trusted the engine clears it and writes a review flag — a line amount the row's own
  quantity and price contradict, a header field printed on every invoice that no reader read, a two-digit year
  outside the sanity window. The screen then says "needs checking" instead of showing a plausible wrong number.

## Rate limits, timeouts and capacity

| Limit | Value | Where |
|---|---|---|
| Administrative writes | 60 per minute per user | `app/main.py` (`limiter`) |
| Password links issued | 20 per ten minutes per user | `app/main.py` |
| Forgotten-password requests | 30 per fifteen minutes per address | `app/main.py` (`public_limiter`) |
| Concurrent uploads being read | 20 slots | `app/main.py` (`slots`) |
| Concurrent local OCR subprocesses | 1 (`INV_LOCAL_OCR_CONCURRENCY`) | `app/engines.py` |
| One reader subprocess | 240 s, 360 s for PaddleOCR (`INV_ENGINE_TIMEOUT`) | `app/engines.py` |
| One reader subprocess, CPU time | the larger of 300 s and 4 × its timeout; address space uncapped unless `INV_STUDIO_READER_MAX_AS_MB` is set | `app/engines.py`, `app/sandbox.py` |
| One job's checks subprocess (`app/job_worker.py`) | each stretch of its own work within `INV_ENGINE_TIMEOUT`, CPU time as a reader; one message to the app at most 64 MB (`INV_STUDIO_JOB_MESSAGE_MB`; blank, 0 or invalid means the default) | `app/engines.py` (`process_job`) |
| Upload check subprocess (pdfium, Pillow) | 10 s CPU, 512 MB address space, 20 s wall clock | `app/upload_check.py` |
| One AI request | 240 s HTTP timeout, 4 pages per chunk | `app/vertex.py` |
| Upload size | 12 MB, 413 above it | `app/main.py` (`MAX_UPLOAD`) |
| Cloud Run request | 300 s, concurrency 20, max 1 instance | service template |

The 300 s request timeout does not cap a read: extraction runs as a background job and the browser polls, so the
longest read measured on the candidate (a 220-line invoice, 482 s when read in three AI chunks) finishes normally.
The limiter is in-memory, which is the whole picture only because the service runs one instance; if max instances
ever rises above one, these limits become per-instance.

**Gap, accepted for this release:** uploading is not rate-limited per user, only bounded by the 20 slots and the
single instance. Membership is invite-only, so the exposure is an invited user uploading in a loop and making the
service slow for the others, not an anonymous flood. Worth a per-user upload limit before the user base grows.

## Health check and startup

- `/health` answers 200 unauthenticated on both the revision URL and the Hosting preview.
- Uptime check `inv-studio /health` runs every 60 s from three regions; alert policies cover the uptime check
  failing, a 5xx ratio above 5 %, container crash / startup probe failure, and Cloud SQL disk above 80 %.
- The startup probe is TCP with a 240 s window, and startup CPU boost is on. OCR models are downloaded and all three
  readers exercised at build time (`scripts/warm_ocr.py`, run in the Dockerfile), so a cold instance has no model
  download to do on the first request.

## Rollback

Runbook section 2 covers both halves (traffic back to a named revision in seconds; re-release the previous Hosting
version). Two things specific to this release:

- Nothing has to be rolled back while the candidate carries no traffic. Removing it is
  `gcloud run services update-traffic inv-studio-api --region us-central1 --remove-tags ocr`, and the preview channel
  expires on its own.
- After the OCR release is promoted, the rollback target is the revision that was live before it, and the database
  needs no step: this release changes no schema. Learned supplier templates live in the evidence bucket and are read
  at startup, so an older revision simply reads the ones it understands.

## Scanning engine settings

- `engine` in the extraction options chooses the reader: `auto` (the default: invoice2data → PaddleOCR → Docling,
  best read by score) or one named reader, with `ai` for the AI read alone.
- `INV_STUDIO_VERIFY_SCANS=1` adds an evidence pass when a scan was read by the AI alone; it is off, and now rarely
  reachable, because the local chain reads scans.
- `INV_STUDIO_AI_CROSS_CHECK=1` makes the AI check a complete local read instead of only filling gaps. It costs a
  call per invoice, so it is off by default and chosen per request.
- Every field records which reader produced it (`readers`), so a disagreement can be traced to the engine that won.
