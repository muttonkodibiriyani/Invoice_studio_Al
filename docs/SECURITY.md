# Security, simply

How Invoice Studio is protected, and what the security review found. Checked against `main` at
`ccc881d` (28 Oct 2026, PR #92). Every file and line below is at that commit.

## Part A. How it is protected

```
 browser ──HTTPS──► Invoice Studio (the API process)
   │                   ├─► child process: first opening of every upload (10 s CPU, 512 MB, 20 s)
   │                   ├─► child process: OCR readers and the multi-invoice check (240 s; PaddleOCR 360 s)
   │                   ├─► child process: every check on the read text, for jobs, review views and learning
   │                   ├─► database, file store, secret store (see the table)
   │                   └─► AI provider: only when an admin connected one and the local readers need help
   └─► Google Firebase: sign-in (cloud only)
```

| | Local (Mac, Windows, Docker) | Google Cloud | Azure |
|---|---|---|---|
| Who can reach it | This computer only. The app listens on `127.0.0.1` (`scripts/run.sh:29`, `scripts/run.cmd:26`); Docker publishes on `127.0.0.1:8080` (`docker-compose.yml:13`). A connection from any other address is refused, whatever the request says (`app/main.py:386`, `app/authorization.py:142-150`). | The internet, over HTTPS: the Firebase Hosting site, which forwards `/api/**` to Cloud Run (`docs/RUNBOOK.md`). The app also accepts Cloud Run's own `*.run.app` address (`app/main.py:302`). | The internet, over HTTPS only (`infra/azure/containerapp.bicep:221-225`). The database has no public address (`infra/azure/postgres.bicep:44`). Key Vault, Storage and the registry do have public addresses, but each request needs an Azure identity with the right role (L4). |
| Sign-in | None. Every request acts as the local operator, who is an admin. That is safe only because nobody else can connect. | Email and password at Google Firebase. The app never sees the password. | The same Firebase sign-in. Azure holds no Google key (`infra/azure/containerapp.bicep:125`). |
| Who gets in | Whoever uses that computer. | People on the admin or member list set at deploy time (`INV_STUDIO_ALLOWED_EMAILS`, `INV_STUDIO_MEMBER_EMAILS`), with a verified email (`app/main.py:314-327`). Admins can also invite people in the app. | The same lists. Admins cannot invite people on Azure; new people come only through the lists. |
| Invoices (uploaded files) | `.data/uploads/` (or the Docker volume), owner-only files | Private bucket | Private Blob container `evidence`; old versions expire after 7 days (`infra/azure/storage.bicep:77-93`) |
| Extracted data, workbooks, audit trail | `.data/studio.sqlite3`, owner-only | Cloud SQL PostgreSQL | PostgreSQL, private network, certificate checked (`infra/azure/main.bicep:187`) |
| Secrets | `.data/vault.key`, next to the database. It decrypts the stored AI keys and ChatGPT login, so any copy of the data folder (a backup, a synced folder, an exported volume) is a secret: encrypt backups and keep them where only you can read them. | Secret Manager | Key Vault, read by the app's own identity |

**Roles in the cloud.** A **member** can upload, read, review and export invoices, and see each invoice's history.
Every member sees every invoice: one deployment serves one organisation (L9). An **admin** can also delete invoices,
connect AI services, change settings and rules, upload templates and reference files, and manage users. Each route
declares its level with `@access(...)` in `app/main.py`, and the app will not start if an API route lacks one
(`app/authorization.py:45`). A user disabled on the Users page is refused on the next request.

**AI keys.** Keys for AI services are stored encrypted with the vault key (`app/store.py:25`, `app/store.py:89`). The
app never sends one back to a browser.

**How an upload is opened.**
1. The API checks the size (12 MB) and that a PDF starts with `%PDF-`.
2. A child process opens the file with pdfium or Pillow, with 10 s of CPU, 512 MB of memory and 20 s in total
   (`app/upload_check.py:12-16`). Images open only as PNG, JPEG, WebP, BMP or TIFF (`app/models.py:16`).
3. The multi-invoice check and the OCR readers run in further child processes, with a time limit and a CPU limit
   (`app/engines.py:242-252`). So does every check on the read text: a job's checks after each read
   (`app/engines.py:329-384`), the review views' text checks (`app/engines.py:300`) and learning at export. A check
   that runs too long stops that one job or check, never the app.
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
  Google whether the caller's token was revoked (`app/main.py:373-375`). On Azure it does this with no key: it sends
  the caller's own token and the public web key. If Google does not answer, the call is refused
  (`app/authentication.py:66-86`, `app/authentication.py:112-116`).
- **Local first run:** the OCR models are downloaded. No invoice data is sent.
- **Nothing else.** The app has no analytics or crash reporting.

## Part B. What the review found, and where each finding stands

**What was checked, and how.**
- **The security review** read the whole codebase at `1a3b121` and ran `pip-audit` on `constraints.txt`. It reproduced
  H1 on a local install only.
- **The security approval pack** read the code again at `26c3d30` and added N1 to N5.
- **This re-check** read the code behind every "fixed" below at `ccc881d` and confirmed that each merge commit is on
  `main`. `pip-audit` on `constraints.txt` reported no known vulnerabilities at `d4b2394`, apart from the three
  accepted `filelock` advisories (`security/accepted-advisories.txt`); `constraints.txt` has not changed since. torch
  and torchvision are not on PyPI, so it skips them.
- **The engine checks** (`tools/security/`, #91 `191455c`) ran on 28 Oct against the installed readers and added
  N6 to N8. The image changes for N6 to N8 were not built here (no Docker build on the review host): the first
  image build proves them, and the engine checks run inside it confirm them (G5).
- Nothing was run against Google Cloud or Azure.

Each finding shows its severity (**H** high, **M** medium, **L** low).

| ID | Sev | What it was | Status on `ccc881d` |
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
| N5 | M | Slow pattern matching over uploaded text inside the API process could hold up the app for everyone. | **Fixed**: #71 `26c3d30`, #75 `9e5c629`, #78 `f126a9e`, #79 `7ec7f9e`, #80 `c88259d`; learning at export, #77 `d4b2394`; the review views' text checks, #83 `cab0c5d`; one learning run at a time, so a deleted invoice's sample cannot come back, #88 `afe6fc7` (`app/learned.py:878`, `app/main.py:277`); every job's checks after the read, #89 `331ae39`. All this work now runs in child processes. The child asks the app for each read, each reader slot and each AI answer, one at a time; the AI keys stay in the app. Each stretch of the child's own work has the readers' time limit (`INV_ENGINE_TIMEOUT`), and each message from it has a size cap (`INV_STUDIO_JOB_MESSAGE_MB`) (`app/engines.py:326-384`). **Left**, accepted by the reviewer: the message cap counts characters, not bytes, so memory is still bounded; the children run as the same user as the app, so this is not file isolation; when a review view's text check is stopped, that check is stored as unavailable for that text, and a new read that gives the same text keeps it unavailable (`app/main.py:484-497`). |
| N6 | M | Docling asked huggingface.co for its layout model on every read. That model is named by a moving branch, so a model changed upstream would be downloaded and used without review. | **Fixed in the image and the template**: #92 `ccc881d`. `HF_HUB_OFFLINE=1` after the model warm-up (`Dockerfile:96`, `Dockerfile.app-layer:19`) and on the Azure app (`infra/azure/containerapp.bicep:117`); the readers receive it (`app/engines.py:220`). Offline, the same read gives the same text and makes no connection. **Left**: a local install outside Docker still asks; new models arrive only with a new image. |
| L1 | L | A deploy input is pasted into a shell script. | **Open**, with M7 (`deploy-azure.yml:44`). |
| L2 | L | CI actions are pinned by tag, and `ci.yml` has no permissions block. | **Open**, with M7. |
| L3 | L | A faked forwarded-address header could dodge rate limits. | **Fixed**: #61 `d385bac`. Check on the Azure front end at first deploy (G7). |
| L4 | L | Key Vault, Storage and the registry have public addresses; vault purge protection was off. | **Partly fixed**: purge protection on, #63 `dff07f5`, so a deleted vault can be recovered for 90 days (`infra/azure/main.bicep:66`, `:71`). Public addresses: **open**, Tarkesh's decision (below). No task. Meanwhile: no shared keys, no anonymous access, no registry admin user, and every access is logged. The app's own identity holds three roles only: Key Vault Secrets User (`infra/azure/keyvault.bicep:72-77`), Storage Blob Data Contributor (`infra/azure/storage.bicep:104-109`) and AcrPull (`infra/azure/registry.bicep:25-30`). Each change must be tried in the real resource group first. |
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
| N7 | L | The image's packaging tools had known vulnerabilities: setuptools 78.1.0 and wheel 0.45.1 (high), pip 25.0.1 (moderate and low). They are pinned in the Dockerfile, not in `constraints.txt`, so no audit saw them. Nothing uses them to handle invoices. | **Fixed**: #92 `ccc881d`. pip 26.2.2, setuptools 84.2.1, wheel 0.48.0 (`Dockerfile:42`); `pip-audit` finds none. **Open**: the CI step that audits these pins is a workflow patch on task `01a18369-6f16` (G4). |
| N8 | L | PaddleX reads model settings with `yaml.FullLoader` (`paddlex/inference/utils/io/readers.py:621`, PaddleX 3.7.2). It refuses the tags that run code, but a settings file can still hand back a Python function. Planting one needs write access to a model folder. | **Fixed in the image**: #92 `ccc881d`. After the warm-up the model folders belong to root and the app's account cannot write to them; the app runs as its own account (`Dockerfile:89-95`, `Dockerfile.app-layer:13-18`). PaddleX still writes its locks and temporary files in `~/.paddlex`, next to them. **Left**: code that already runs as the app's account could rename the folder and put its own in its place, but it would not need the model file then. A local install keeps the models in the user's own folders. The loader is PaddleX's own code. |

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
| G4 | Someone with the GitHub workflow permission applies the CI and deploy-workflow patches (M7, L1, L2, M5, N7). | Open |
| G5 | Deploy templates and image from `ccc881d` (the commit that closes G8), or later. Before go-live, run one read in the final image, offline and with the models locked, or run the engine checks (`tools/security/`) inside it: no build step reads a document after those two steps (N6, N8). | Open: waits for the release 1 freeze (task `01a18369-754a`) and the first image build |
| G6 | After the first deploy, storage shows the rule `expirePreviousVersions`. | Open: needs the deployment |
| G7 | Every first-deploy check in `docs/AZURE.md` passes. | Open: needs the deployment |
| G8 | No pattern work over uploaded text runs in the API process without a time limit (N5). | **Met in the code** at `ccc881d`: #77 `d4b2394`, #83 `cab0c5d`, #88 `afe6fc7`, #89 `331ae39`. Open: it holds on Azure only once an image from `ccc881d` or later is deployed (G5, task `01a18369-754a`) |
| G9 | The first Azure deploy runs with no Google project ID we do not own and no bootstrap email a token could claim, and the app's ingress stays internal-only until the sign-in redeploy. Why: the Azure first-deploy runbook (tm8 plan page for graph 01a18177-9fd4, step A4) sets `GOOGLE_CLOUD_PROJECT` to a placeholder project ID that we do not own (the template example is a placeholder too, `infra/azure/main.parameters.example.json:9`). The app accepts Google-signed sign-in tokens for whatever project that setting names (`app/authentication.py:28`, `:59-63`, `:113`), and gives a first sign-in with a verified email on the bootstrap list that list's role (`app/main.py:317-319`). Whoever registered that project ID could sign in as a bootstrap admin. Admin routes and writes would still be refused, because the keyless account lookup fails with a placeholder web API key (`app/main.py:374-375`), but member reads would pass, so invoices could be read. Ingress is external today (`infra/azure/containerapp.bicep:222`). | **Open: blocks the Azure first deploy.** From the Graph Architect (tm8 msg `01a18391-a0a4`) |

**Decisions only Tarkesh can make:**
1. When to move the live Google Cloud service to a build that has these fixes.
2. Go-ahead to deploy on Azure, and whether Azure runs next to Google Cloud or replaces it.
3. Who gets the GitHub workflow permission, or whether to apply the patches yourself (G4).
4. Which AI providers production may use. Any provider an admin connects receives invoices.
5. Private network addresses for Key Vault and Storage, about US$7.30 a month each, plus about US$0.50 a
   month for each private DNS zone. The registry needs the Premium tier, about US$51 a month (L4).
6. A web firewall or an IP allow-list in front of the app.
7. Accept two exceptions to the security standard: sign-in tokens kept in browser storage (Firebase's own library
   does this), and roles kept in the app instead of the identity provider.
8. Sign off the coordinator's accepted risks: L9, L13, the `filelock` advisories, and the rest of M4.
9. Turn on branch protection, so `main` needs a passing review and passing checks.
10. Check the live Google Cloud bucket for a rule that expires old file versions. Without one, deleted invoices stay
    for ever: the N1 problem on Google Cloud. Nobody in this work had access to check.
