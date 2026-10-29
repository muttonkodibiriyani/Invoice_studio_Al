# Security, simply

How Invoice Studio is protected, and what the security review found. Checked against `main` at
`d4b2394` (28 Oct 2026, PR #77). Every file and line below is at that commit.

## Part A. How it is protected

```
 browser ──HTTPS──► Invoice Studio (the API process)
   │                   ├─► child process: first opening of every upload (10 s CPU, 512 MB, 20 s)
   │                   ├─► child process: OCR readers and the multi-invoice check (240 s; PaddleOCR 360 s)
   │                   ├─► database, file store, secret store (see the table)
   │                   └─► AI provider: only when an admin connected one and the local readers need help
   └─► Google Firebase: sign-in (cloud only)
```

| | Local (Mac, Windows, Docker) | Google Cloud | Azure |
|---|---|---|---|
| Who can reach it | This computer only. The app listens on `127.0.0.1` (`scripts/run.sh:29`, `scripts/run.cmd:26`); Docker publishes on `127.0.0.1:8080` (`docker-compose.yml:13`). A connection from any other address is refused, whatever the request says (`app/main.py:385`, `app/authorization.py:142-150`). | The internet, over HTTPS: the Firebase Hosting site, which forwards `/api/**` to Cloud Run (`docs/RUNBOOK.md`). The app also accepts Cloud Run's own `*.run.app` address (`app/main.py:301`). | The internet, over HTTPS only (`infra/azure/containerapp.bicep:215-219`). The database has no public address (`infra/azure/postgres.bicep:44`). Key Vault, Storage and the registry do have public addresses, but each request needs an Azure identity with the right role (L4). |
| Sign-in | None. Every request acts as the local operator, who is an admin. That is safe only because nobody else can connect. | Email and password at Google Firebase. The app never sees the password. | The same Firebase sign-in. Azure holds no Google key (`infra/azure/containerapp.bicep:119`). |
| Who gets in | Whoever uses that computer. | People on the admin or member list set at deploy time (`INV_STUDIO_ALLOWED_EMAILS`, `INV_STUDIO_MEMBER_EMAILS`), with a verified email (`app/main.py:313-326`). Admins can also invite people in the app. | The same lists. Admins cannot invite people on Azure; new people come only through the lists. |
| Invoices (uploaded files) | `.data/uploads/` (or the Docker volume), owner-only files | Private bucket | Private Blob container `evidence`; old versions expire after 7 days (`infra/azure/storage.bicep:77-93`) |
| Extracted data, workbooks, audit trail | `.data/studio.sqlite3`, owner-only | Cloud SQL PostgreSQL | PostgreSQL, private network, certificate checked (`infra/azure/main.bicep:187`) |
| Secrets | `.data/vault.key`, next to the database. It decrypts the stored AI keys and ChatGPT login, so any copy of the data folder (a backup, a synced folder, an exported volume) is a secret: encrypt backups and keep them where only you can read them. | Secret Manager | Key Vault, read by the app's own identity |

**Roles in the cloud.** A **member** can upload, read, review and export invoices, and see each invoice's history.
Every member sees every invoice: one deployment serves one organisation (L9). An **admin** can also delete invoices,
connect AI services, change settings and rules, upload templates and reference files, and manage users. Each route
declares its level with `@access(...)` in `app/main.py`, and the app will not start if an API route lacks one
(`app/authorization.py:45`). A user disabled on the Users page is refused on the next request.

**AI keys.** Keys for AI services are stored encrypted with the vault key (`app/store.py:30`). The app never sends one
back to a browser.

**How an upload is opened.**
1. The API checks the size (12 MB) and that a PDF starts with `%PDF-`.
2. A child process opens the file with pdfium or Pillow, with 10 s of CPU, 512 MB of memory and 20 s in total
   (`app/upload_check.py:12-16`). Images open only as PNG, JPEG, WebP, BMP or TIFF (`app/models.py:16`).
3. The multi-invoice check and the OCR readers run in further child processes, with a time limit and a CPU limit
   (`app/engines.py:242-252`).
4. Children get a short list of environment variables, with no API keys or tokens (`app/engines.py:216`).
5. On Linux the API process marks itself so that a child cannot read its memory or environment (`app/sandbox.py:21-25`).

Where this is weaker:
- **Windows:** the CPU and memory caps do not apply, only the time limits (`app/sandbox.py:4`). File permission
  calls have no effect there either, so keep the data folder out of shared or synced folders.
- **macOS:** the caps apply, but the protection in step 5 is Linux-only. Docker on any computer runs Linux, so it
  gets everything.
- **Everywhere:** the readers run as the same user as the app. Splitting a multi-invoice PDF and drawing pages for
  the AI still happen inside the API process.

**What leaves the machine, and when.**
- **Invoice content goes to an AI provider only if** an admin connected one and one of these is true: the local
  readers left a gap or the totals do not add up (with AI fallback on); a person picked the AI as the reader;
  or a person turned on cross-check (`docs/AI_BOUNDARY.md`).
  - On Google Cloud the provider can be Vertex AI, which uses the service's own identity, so no key is stored.
  - On Azure it is the Azure AI Foundry endpoint the operator sets.
  - An admin can also connect OpenAI, Anthropic, Claude or ChatGPT. That sends invoices to that company, and the
    change is audited.
- **Sign-in (cloud only):** the browser talks to Google Firebase. On every write or admin call the app also asks
  Google whether the caller's token was revoked (`app/main.py:372-374`). On Azure it does this with no key: it sends
  the caller's own token and the public web key. If Google does not answer, the call is refused
  (`app/authentication.py:66-86`, `app/authentication.py:112-116`).
- **Local first run:** the OCR models are downloaded. No invoice data is sent.
- **Nothing else.** The app has no analytics or crash reporting.

## Part B. What the review found, and where each finding stands

**What was checked, and how.**
- **The security review** read the whole codebase at `1a3b121` and ran `pip-audit` on `constraints.txt`. It reproduced
  H1 on a local install only.
- **The security approval pack** read the code again at `26c3d30` and added N1 to N5.
- **This re-check** read the code behind every "fixed" below at `d4b2394` and confirmed that each merge commit is on
  `main`. `pip-audit` on `constraints.txt` reports no known vulnerabilities, apart from the three accepted `filelock`
  advisories (`security/accepted-advisories.txt`). torch and torchvision are not on PyPI, so it skips them.
- Nothing was run against Google Cloud or Azure.

Each finding shows its severity (**H** high, **M** medium, **L** low).

| ID | Sev | What it was | Status on `d4b2394` |
|---|---|---|---|
| H1 | H | Anyone on the same network as a Docker install could act as its admin. | **Fixed**: #43 `4e49d69`, #56 `9a519ac`. |
| H2 | H | The Azure template put an admin key for the Firebase user pool, shared with Product Intelligence, into the app. | **Fixed**: #44 `10aa0d0`, #48 `92bdaeb`, #54 `22980b7`. No such key in `app/` or `infra/`. |
| H3 | H | Pillow, which opens uploads, had 38 known vulnerabilities. | **Fixed**: #50 `f425d58`. Pillow 13.0.0 and an image-format allow-list. |
| M1 | M | The Azure database was open to other Azure customers, the app logged in as its admin, and the certificate was not checked. | **Fixed in the template**: #42 `f9812b3`, #63 `dff07f5`. Moving the app to its limited login is a deploy step: **open, G1**. |
| M2 | M | The Azure template's setting names did not match what the app reads. | **Fixed**: #42 `f9812b3`, #54 `22980b7`. |
| M3 | M | A test-only sign-in shortcut was blocked on Google Cloud but not on Azure. | **Fixed**: #51 `8568bfc` (`app/authentication.py:45-46`). |
| M4 | M | Uploads were first opened inside the API process, and readers had no resource limits. | **Fixed**: #69 `9424b62`, #78 `f126a9e`. **Rest deferred** by the coordinator: PDF splitting and AI page drawing in the API process, readers as the same user. No task. |
| M5 | M | A new version under test could take over invoices the live version was processing. | **Fixed**: #72 `c4d90d1`, #79 `7ec7f9e`, #82 `3878804`. The matching change to the deploy workflow is posted on task `01a182b3-4fac` and needs the workflow permission (G4). |
| M6 | M | Other libraries had known vulnerabilities. | **Fixed**: #60 `78b44bb`. **Accepted** by the coordinator: three `filelock` advisories in code nothing here calls. |
| M7 | M | CI runs no security checks and does not install the pinned versions. | **Open.** `ci.yml` is unchanged. A patch is on task `01a181f9-3b1e`. It needs someone with the GitHub workflow permission (G4). |
| M8 | M | The ChatGPT login export was an unencrypted file. | **Fixed**: #66 `4b2c64f`. Sealed under a one-time passphrase, and audited. |
| N1 | M | On Azure, a deleted invoice stayed in storage for ever as an old file version. | **Fixed in the template**: #70 `a943a66`. Confirm after the first deploy: G6. |
| N5 | M | Slow pattern matching over uploaded text inside the API process could hold up the app for everyone. | **Partly fixed**: #71 `26c3d30`, #75 `9e5c629`, #78 `f126a9e`, #79 `7ec7f9e`, #80 `c88259d`, and learning at export #77 `d4b2394`. **Open**: a per-supplier lock on learning, without which a deleted invoice's sample can come back (task `01a182b3-4d38`); review views (#83, task `01a182b3-4617`); checks after each read (task `01a182b3-4377`). |
| L1 | L | A deploy input is pasted into a shell script. | **Open**, with M7 (`deploy-azure.yml:44`). |
| L2 | L | CI actions are pinned by tag, and `ci.yml` has no permissions block. | **Open**, with M7. |
| L3 | L | A faked forwarded-address header could dodge rate limits. | **Fixed**: #61 `d385bac`. Check on the Azure front end at first deploy (G7). |
| L4 | L | Key Vault, Storage and the registry have public addresses; vault purge protection was off. | **Partly fixed**: purge protection on, #63 `dff07f5`. Public addresses: **open**, Tarkesh's decision (below). No task. Meanwhile: no shared keys, no anonymous access, no registry admin user, and every access is logged. Each change must be tried in the real resource group first. |
| L5 | L | The local vault key sits next to the database. | **Partly fixed**: documented as a secret, #64 `6cb880f` (now in Part A). OS keychain deferred. No task. |
| L6 | L | An AI model name could start with `-`. | **Fixed**: #57 `6df685b`. |
| L7 | L | An audit view read the whole audit table. | **Fixed**: #62 `b39344d`. |
| L8 | L | Connecting an AI key was not audited. | **Fixed**: #58 `0d00a7f`. |
| L9 | L | Every member sees every invoice. | **Accepted** by the coordinator: one organisation per deployment. |
| L10 | L | Error messages could show internal details. | **Fixed**: #59 `27ec1bf`. |
| L11 | L | Packages are installed without hash checks. | **Partly fixed**: base image pinned by digest, #60 `78b44bb`. Hash checks deferred. No task. |
| L12 | L | The bundled sign-in script had no recorded version. | **Fixed**: #60 `78b44bb`. |
| L13 | L | Someone removed only in Firebase can keep reading for up to an hour. | **Accepted** by the coordinator: disabling a person on the Users page applies on the next request. |
| L14 | L | The Azure guide's test address was refused by the app. | **Fixed**: #54 `22980b7`. |
| N2 | L | No HSTS header. | **Fixed**: #70 `a943a66`, cloud only. |
| N3 | L | Changes to AI settings and ChatGPT accounts were not audited. | **Fixed**: #73 `6fc69f0`. |
| N4 | L | An unexpected server error came back without the security headers. | **Fixed**: #73 `6fc69f0`. |

The coordinator who made the decisions above is the Senior SecOps Engineer agent. Tarkesh has not signed them
off yet.

## What is left before real users

**Google Cloud is already live, on an older build.** `docs/RUNBOOK.md` says it runs `3633d1c` (23 Oct). That build
is older than the security review, so none of the fixes above runs there. This was not checked against the live
service.

**Go-live conditions for Azure** (from the approval pack):

| # | Condition | Today |
|---|---|---|
| G1 | Run `docs/AZURE.md` §5a once, so the app uses its limited database login. `scripts/azure_db_app_role.py check` must print OK. | Open: needs the deployment |
| G2 | Every later template run uses `writeSecrets=false`. | Open: an operating rule |
| G3 | Required reviewers on the GitHub `azure` environment. | Open: a repository setting |
| G4 | Someone with the GitHub workflow permission applies the CI and deploy-workflow patches (M7, L1, L2, M5). | Open |
| G5 | Deploy templates and image from the commit that closes G8, or later. | Open: waits for G8 |
| G6 | After the first deploy, storage shows the rule `expirePreviousVersions`. | Open: needs the deployment |
| G7 | Every first-deploy check in `docs/AZURE.md` passes. | Open: needs the deployment |
| G8 | No pattern work over uploaded text runs in the API process without a time limit (N5). | Open: #83, tasks `01a182b3-4617`, `01a182b3-4377`, `01a182b3-4d38` |

**Decisions only Tarkesh can make:**
1. When to move the live Google Cloud service to a build that has these fixes.
2. Go-ahead to deploy on Azure, and whether Azure runs next to Google Cloud or replaces it.
3. Who gets the GitHub workflow permission, or whether to apply the patches yourself (G4).
4. Which AI providers production may use. Any provider an admin connects receives invoices.
5. Private network addresses for Key Vault and Storage, about US$7.30 a month each. The registry needs the Premium
   tier, about US$51 a month (L4).
6. A web firewall or an IP allow-list in front of the app.
7. Accept two exceptions to the security standard: sign-in tokens kept in browser storage (Firebase's own library
   does this), and roles kept in the app instead of the identity provider.
8. Sign off the coordinator's accepted risks: L9, L13, the `filelock` advisories, and the rest of M4.
9. Turn on branch protection, so `main` needs a passing review and passing checks.
10. Check the live Google Cloud bucket for a rule that expires old file versions. Without one, deleted invoices stay
    for ever: the N1 problem on Google Cloud. Nobody in this work had access to check.
