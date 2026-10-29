# Invoice Studio

Azure migration baseline: see [the implementation order and remaining deployment gates](docs/AZURE_MIGRATION.md).

Read supplier invoices, check them against your reference data, resolve exceptions and download the invoice workbook: three template sheets, plus a Checks sheet when it is checked against your reference files. Runs locally or in a restricted Firebase/GCP pilot. The repository contains synthetic examples only.

**Hosted portal:** https://inv-studio-740495548022.web.app — sign in with an approved owner account. What is live, and how releases are made, is recorded in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Run it locally

The local install needs no account and no cloud: no sign-in, SQLite, files on disk under `.data/`. Python 3.12 is the tested runtime; macOS needs version 11 (Big Sur) or newer.

**macOS / Linux (Terminal):**

```bash
git clone https://github.com/muttonkodibiriyani/Invoice_studio_Al.git inv-studio && cd inv-studio
./scripts/setup.sh
./scripts/run.sh
```

**Windows (Command Prompt):**

```bat
git clone https://github.com/muttonkodibiriyani/Invoice_studio_Al.git inv-studio
cd inv-studio
scripts\setup.cmd
scripts\run.cmd
```

Open **http://127.0.0.1:8765**. The setup script creates `.venv`, installs the application and its tests, and copies `.env.example` to `.env` — the one list of configuration variables, shared with the cloud deployment. Leave it blank to run locally; `scripts/setup.sh` uses [uv](https://docs.astral.sh/uv/) when it is installed and plain `venv`/`pip` otherwise.

**Docker:**

```bash
docker compose up
```

Open **http://localhost:8080**. This builds the same image the cloud runs (so the two local OCR engines are included and pre-warmed; the first build downloads several GB) and keeps invoice data in a named volume. Compose puts the folder name in front of the volume name, so a checkout in `inv-studio` stores it in `inv-studio_inv-studio-data` (`docker volume ls` lists it).

Local mode has no sign-in: whoever reaches it is the administrator. So it only answers this computer. Compose publishes the port on `127.0.0.1` only, and the app refuses connections from any other address. Do not publish it on another interface or put it behind a proxy. To share the app, use the cloud mode with sign-in. On Docker Desktop (macOS, Windows) other computers can reach the container from the same address as this one, so the address check cannot tell them apart; the `127.0.0.1` publish is what protects you there.

### Scanned PDFs and photos: PaddleOCR and Docling

The basic install reads digital documents: invoice2data templates and the readable-PDF fast path. Scanned PDFs and photos need the two scan readers. Add them with `--ocr`, which also checks every reader on the sample invoices:

```bash
./scripts/setup.sh --ocr
.venv/bin/python scripts/check_engines.py      # checks the readers again at any time
```

```bat
scripts\setup.cmd --ocr
.venv\Scripts\python scripts\check_engines.py
```

| Computer | Scan readers |
|---|---|
| Windows 10 or 11, 64-bit | PaddleOCR and Docling |
| Mac with Apple silicon, macOS 14 or newer | PaddleOCR and Docling |
| Mac with Apple silicon, macOS 11 to 13 | PaddleOCR |
| Intel Mac, macOS 12 or newer | PaddleOCR, on paddlepaddle 3.0.0 (the last release with an Intel Mac build) |
| Linux x86_64 | PaddleOCR and Docling |

Docling needs PyTorch, which ships for Apple silicon on macOS 14 or newer only, and no longer for Intel Macs. Where Docling is missing, the cloud version reads with both. A scan reader takes one to two minutes on a scanned invoice. On a slow computer, if a read stops at its time limit, set `INV_ENGINE_TIMEOUT=900` in `.env`.

You need Python 3.12 (64-bit; on Windows the python.org installer with "Add python.exe to PATH"), about 8 GB of free disk (counting the download cache, temporary and model folders, wherever they are), 8 GB of memory, and the internet on the first run, which downloads about 600 MB of public OCR models. Windows also needs the [Microsoft Visual C++ Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe), which PyTorch and PaddlePaddle load; setup stops and says so when it is missing. Tesseract, Poppler, a GPU and a compiler are not needed. `scripts/install_ocr.py` installs the versions the cloud image runs (`constraints.txt`), CPU-only.

**If the model download is refused** (`LocalEntryNotFoundError`, a refused or timed-out connection, `WinError 10013`): the reader is installed and nothing is wrong with the sample — the network would not let it fetch its models. A company network or VPN often blocks the model hosts. One reader working is enough to read scans, so the install keeps going and names the one that is stuck. Fix the network, or name your proxy, and then re-run the check alone — no reinstall:

```bash
export HTTPS_PROXY=http://proxy.example.com:8080
.venv/bin/python scripts/check_engines.py --any paddleocr docling
```

```bat
set HTTPS_PROXY=http://proxy.example.com:8080
.venv\Scripts\python scripts\check_engines.py --any paddleocr docling
```

The models are downloaded once; after that the readers work offline.

For a readable PDF with no matching supplier template, the automatic path keeps the native text and skips the expensive image-OCR pass; it can continue to the selected AI provider when the confirmed plan permits fallback. The job view shows the active stage instead of presenting the whole chain as one wait. No invoice is sent to an AI provider unless you enable fallback or select AI and confirm the processing plan. Credentials are encrypted on the server and never stored in browser storage.

To try the app with the synthetic demo reference set, start it with `INV_STUDIO_DEMO_REFERENCES=1` (test-only; the server refuses it in cloud mode) and choose **Try synthetic demo**.

## AI readers

A reader is offered once it is connected. OpenAI, Anthropic, ChatGPT and Claude Code are connected per-operator on the **Engines & AI** page; the others are configuration (see `.env.example`).

| Reader | What to supply |
|---|---|
| OpenAI | API key, on the Engines & AI page. |
| Anthropic | API key, on the Engines & AI page. |
| Azure AI Foundry | Resource endpoint and key, on the Engines & AI page — or pre-connect with `INV_STUDIO_AZURE_ENDPOINT` / `INV_STUDIO_AZURE_API_KEY`. Select your **deployment name** as the model. |
| ChatGPT | Sign in with the application's own OAuth registration. An explicit export/import moves the encrypted credential to the hosted workspace and disconnects the local copy. |
| Claude Code | A Claude setup token connects the restricted Claude CLI on a server where that CLI is installed. |
| Vertex AI | `VERTEX_PROJECT_ID` / `VERTEX_LOCATION` / `VERTEX_MODEL`, through the runtime service identity (locally: application-default credentials). No API key. |

Provider/account eligibility, model access and usage limits apply.

## What works

- Multiple files, each containing one invoice: PDF, PNG, JPEG, WebP, BMP, TIFF, DOCX, XLSX, CSV, TXT and structured invoice JSON.
- Confirm the engine, fallback provider/model, reference version and tolerances before processing. Cancel makes no processing request.
- Automatic reading through supplier templates, a readable-PDF fast path, PaddleOCR and Docling, with optional selected AI fallback for unread or incomplete invoices. You can select a particular reader or AI explicitly, and the UI reports each active stage.
- Canonical reference import and strict matching remain the approval path. A separate, read-only product/PO lookup indexes large source extracts for exact identifier or product-name evidence searches. Search results retain source/conflict context and require explicit operator confirmation; they do not become approved matching data.
- Editable evidence review, duplicate protection, cumulative receipt allocation and atomic approved export.
- Exactly **Header (13 columns), Tax_Breakdown (3), Details (6)**. With your real reference files (`rules_workbook`, then `with_checks_sheet` in `app/main.py`), the approved export adds a fourth sheet, **Checks**: each cell's status with its evidence, and the row and workbook checks. The template sheets are unchanged. With the synthetic demo references (`INV_STUDIO_DEMO_REFERENCES=1`) the export has the three template sheets only. Approved single/batch exports revalidate and reserve receipts. A separate manual editor exposes every target field and can immediately download an explicitly acknowledged `DRAFT_UNVALIDATED` workbook while OCR is still running. That draft does not validate references, reserve receipts or change the job.

## Your reference files

Download the reference template in the portal. The canonical workbook has Suppliers, Routes, Items, POs, POLines, Receipts and TaxRules sheets. Canonical JSON and ODS equivalents are accepted. A completed target invoice workbook is detected and rejected as a reference file.

Large business Item Master and PO/GRN extracts need a mapped staging step; do not rename their tabs and assume they are approved references. Supplier entity, site, buying company, route, currency, receipt status and baseline already-invoiced quantity must be source-proven or explicitly supplied by the data owner. Source files, private analysis, keys and `.data` must not be committed.

The lookup index helps an operator find a product or PO record in those large extracts without loading it into the approval engine. Confirming a lookup result copies evidence into the manual form only; it does not approve a supplier, route, item, receipt or invoice for the guarded export.

## Verify

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

`./scripts/setup.sh` installs `.[test]`. A few tests (`tests/test_keyless_identity.py`) also need the cloud extra (`firebase_admin`), as CI installs it; without it they error with `ModuleNotFoundError`. To run the full suite as CI does:

```bash
uv pip install --python .venv -e '.[test,cloud]'    # or: .venv/bin/python -m pip install -e '.[test,cloud]'
.venv/bin/pytest -q
```

One test skips unless a private local invoice set is configured, and a few script checks also skip when Node.js is not installed. Use a separate data directory for manual testing:

```bash
INV_STUDIO_DATA=/tmp/inv-studio-test ./scripts/run.sh
```

CI runs the same ruff and pytest commands on every push and pull request (`.github/workflows/ci.yml`).

The golden-sample tests prove the samples are read locally with no AI call. The two scans' tests need the scan readers: `.venv/bin/pytest -q -m scan_readers` after `./scripts/setup.sh --ocr` (`docs/GOLDEN_SAMPLES.md`).

The UI is edited in `app/static/` only. `hosting/public/` (what Firebase Hosting serves) is derived from it by `scripts/sync_static.py`, and a test fails when the two drift.

## Operations

- [Security](docs/SECURITY.md): how each install is protected, and the status of every security finding.
- [Runbook](docs/RUNBOOK.md): what is live, how a candidate is deployed, released and rolled back.
- [Azure, simply](docs/AZURE.md#azure-simply): what runs on Azure, how an invoice's data moves, and the monthly cost. The full Azure guide follows it.
- [Recovery](docs/RECOVERY.md).
- [Production readiness](docs/PRODUCTION_READINESS.md).
- [Deploy identity plan](docs/DEPLOY-IDENTITY-PLAN.md).

## Operational boundaries

The local mode is a single-operator MVP bound to loopback. The hosted pilot adds Firebase authentication, an owner allowlist, Cloud SQL, private Cloud Storage and Secret Manager. It still uses one Cloud Run instance and an in-process queue. Drain jobs before deployments; interrupted work requires a confirmed retry. A ChatGPT credential bundle is a one-time ownership transfer: successful export removes the local encrypted copy, so protect the file until the hosted import succeeds and delete it afterward. How each install is protected, and where every security finding stands, is in [Security](docs/SECURITY.md). Add role separation, durable distributed workers, schema migrations, backup restoration tests, monitoring and business acceptance before a wider rollout. Neither ledger detects invoices posted in another system unless that history is imported.

Invoice limits: 12 MB, 20 PDF pages or image frames, 25 megapixels per image, 1,000 structured lines. Office archives must expand to at most 30 MB. Multi-invoice PDFs must be split first. Credit notes, multiple POs, mixed tax rates, unit conversions, freight and discount allocation need dedicated contracts before automatic export. Unsupported or ambiguous cases remain on hold.

The application code is MIT licensed. Reader libraries and downloaded model weights retain their own licenses; review them before redistribution.
