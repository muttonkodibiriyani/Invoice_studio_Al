# Engine security checks

Three scripts, one per engine that reads invoices in Invoice Studio. Each runs on its own, needs only the
Python standard library, and changes nothing on the machine it runs on.

| Script | Engine | What it reads in the app |
|---|---|---|
| `check_invoice2data.py` | invoice2data 1.0.1 | supplier templates over the text of readable PDFs, and over OCR text |
| `check_paddleocr.py` | PaddleOCR 3.7.0, PaddleX 3.7.2, PaddlePaddle 3.3.1 | scanned PDFs and photos |
| `check_docling.py` | Docling 2.143.1 (+ RapidOCR, PyTorch) | layout and tables, scans included |

## How to run

Use the app's own Python, so the check sees exactly what the app runs:

```bash
# macOS / Linux, from the repository folder
.venv/bin/python tools/security/check_invoice2data.py
.venv/bin/python tools/security/check_paddleocr.py
.venv/bin/python tools/security/check_docling.py
```

```bat
:: Windows (Command Prompt), from the repository folder
.venv\Scripts\python tools\security\check_paddleocr.py
```

```bash
# The production image, with no network at all (strongest test; --offline skips the vulnerability lookup)
docker run --rm -i --network none --entrypoint python <image> - --offline < tools/security/check_docling.py
```

A full run takes about 30 s (invoice2data) or 2 minutes (PaddleOCR, Docling). `--quick` skips the slow
parts, `--help` lists every option. Exit status: 0 no failure, 1 a failure (or a warning with `--strict`),
2 could not run. Each run also writes a JSON report with the evidence.

To pin the model files: run once on a trusted build with `--write-manifest models.json`, keep the file,
and later run `--compare-manifest models.json` on any machine or container. A changed model fails.

## What each check does

All three:
- **Versions:** the installed engine and its key libraries match `constraints.txt`, the versions the
  security review covered.
- **Tampering:** every installed file still matches the hash pip recorded at install, and nothing extra
  was planted in the engine's folders or as a `.pth` start-up hook.
- **Known vulnerabilities:** the engine and everything it depends on, looked up at api.osv.dev (only
  package names and versions are sent). Advisories already accepted in
  `security/accepted-advisories.txt` are shown as accepted.
- **Secrets:** test values planted in 15 secret variables (vault key, database URL, AI keys, cloud
  credentials) must not reach the reader process the app starts.
- **Limits:** the reader runs with CPU and memory limits, and on Linux the API process hides its memory
  from it.
- **A real read with the network blocked:** the engine reads a sample invoice exactly the way the app
  does (`python -m app.ocr_worker`), while a recorder refuses every non-local connection and logs every
  connection attempt, every program started and every file written.
- **Risky-code inventory:** where the engine's own code loads unsafe formats, starts programs or uses the
  network, plus the web addresses it names (for an egress allow-list).

Engine-specific:
- **invoice2data:** trap templates that try to run code must load as nothing; only an administrator can
  upload a template; invoice2data's own AI fallback stays off; a template with a catastrophic regular
  expression must be stopped by the app's time limit.
- **PaddleOCR:** model files must be data formats (no pickle, no code); PaddleX's settings loader is tested
  with trap files; model hosts are listed; an image bomb, a PDF with JavaScript and a canary link, and a
  damaged PDF must fail fast and contact nothing.
- **Docling:** model files are checked against the hash in their Hugging Face cache names; `torch.load`
  and `trust_remote_code` paths are checked against what the app uses; the Hugging Face settings are
  checked; the same hostile files.

## What the first run found (28 Oct 2026, Linux, pinned versions, models cached)

| | invoice2data | PaddleOCR | Docling |
|---|---|---|---|
| Result | 11 pass, 0 warn, 0 fail | 10 pass, 1 warn, 1 fail | 9 pass, 2 warn, 1 fail |
| Network during a read | none | none | 2 calls to huggingface.co per read |

1. **Docling checks Hugging Face on every read.** Its layout model follows the moving `main` branch, so
   with the network open, a model changed upstream would be downloaded and used without review. With
   `HF_HUB_OFFLINE=1` the same read gave the same result and made no call. Fix: set `HF_HUB_OFFLINE=1`
   for the app after the image's model warm-up.
2. **Packaging tools in the image have known advisories**, and nothing audits them because they are
   pinned in the Dockerfile, not in `constraints.txt`: setuptools 78.1.0 (high, fixed in 78.1.1),
   wheel 0.45.1 (high, fixed in 0.46.2), pip 25.0.1 (moderate and low, fixed by 26.2). The app does not
   use them to handle invoices. Fix: raise the three pins in the Dockerfile and audit them in CI.
3. **PaddleX reads model settings (`inference.yml`) with `yaml.FullLoader`**, not the safe loader. The
   code-running tags were refused, but a settings file can still hand back a real Python function. It
   needs write access to the model folder. Fix: model folders read-only for the app's account, and the
   manifest check above.
4. **PaddlePaddle runs four shell probes when it loads** (`ldd --version`, `cat /proc/cpuinfo`,
   `which nvcc`, `which ccache`). They are harmless, but a strict runtime policy will see them.
