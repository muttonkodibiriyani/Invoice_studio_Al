# Golden-sample tests: the local readers read the samples, the AI is never asked

`tests/test_golden_samples.py` runs `process()` in `app/engines.py` with the real readers on each sample and compares
what they read with the sample's expected fields. Only the AI reader is replaced. It records the call, so a test
fails whenever the AI is asked to read a sample the local readers can read.

| Sample | Expected fields | Read by | Needs | Runs in |
|---|---|---|---|---|
| `samples/invoice.pdf` | `samples/invoice.json` | invoice2data (its demo template) | the base install | `pytest -q` |
| `samples/invoice-2.pdf` | `samples/invoice-2.json` | invoice2data | the base install | `pytest -q` |
| `samples/invoice-scan.png` | `samples/invoice-scan.json` (the same invoice as `invoice.pdf`, printed as a scan) | PaddleOCR, then Docling | `--ocr` and the models | `pytest -q -m scan_readers` |
| `samples/plain-invoice.pdf` | `samples/plain-invoice.json` | invoice2data, with no supplier template (the template-free parser, C1) | the base install | `pytest -q` |
| `samples/plain-invoice-scan.png` | `samples/plain-invoice-scan.json` (the same invoice as `plain-invoice.pdf`, printed as a scan; see below) | PaddleOCR, then Docling | `--ocr` and the models | `pytest -q -m scan_readers` |

The expected fields are what the local readers read, checked by eye against the printed invoice. The two plain
invoices print line 2 as `Gel pen blue 0.5 mm`. PaddleOCR reads the scan as `0.5mm`, without the space, so
`plain-invoice-scan.json` expects `0.5mm`. The comparison is exact for text fields (amounts compare as numbers),
so the test fails if a reader starts reading that line differently, and the expected value is then updated on
purpose, not loosened.

Another test reads `invoice.pdf` with no supplier template and the scan readers made unavailable. It checks the
two exits the diagnosis found (`docs/AI_BOUNDARY.md`, #52). With no sound read, the 250-character exit must not skip
PaddleOCR and Docling. A reader that is not installed must be named on the job with its install hint, as the
reason the AI ran. Putting either exit back fails this test, and the matching stub tests in
`tests/test_reader_chain.py` fail too.

A reader a test needs that is not installed fails the test with the install hint. It never skips. The scan tests
carry the `scan_readers` marker, and `pyproject.toml` leaves that marker out of a plain `pytest -q`, because the
base install has no scan readers. `-m scan_readers` on the command line replaces that default.

## Running them locally

```bash
./scripts/setup.sh --ocr                                       # once: the readers, and their models (about 600 MB)
.venv/bin/pytest -q tests/test_golden_samples.py               # the PDFs and the two exits
.venv/bin/pytest -q -m scan_readers tests/test_golden_samples.py   # the two scans, about 70-90 s each on 2 CPUs
```

## How CI gets the readers and models, with no network at test time

The scan test runs in its own job, `golden-scan`. The default `test` job stays on the base install.

1. The job installs the readers with `scripts/install_ocr.py --uv`, at the versions pinned in `constraints.txt`.
   This is the same installer `./scripts/setup.sh --ocr` uses, so CI tests what a user installs.
2. The models are kept in the job's workspace (`HF_HOME` for Docling, `PADDLE_PDX_CACHE_HOME` for PaddleOCR) and
   cached with `actions/cache`, keyed on `constraints.txt`. On a cache hit, nothing is downloaded. In every case
   the installer's own check (`scripts/check_engines.py`) reads the samples once while the network is still up,
   so a model a newer reader needs is fetched before the tests start.
3. The tests then run inside a network namespace with only loopback (`sudo unshare --net`), as the runner user,
   with `HF_HUB_OFFLINE=1`. A step first proves that the namespace cannot reach the model host. A reader that
   tries to download anything fails the test.
4. The job uses no secrets. The readers run in `app.ocr_worker` with an allow-listed environment
   (`engines.local_read`), and the AI reader is the test's recorder, so no provider key exists to be read.

The app image (`Dockerfile`) bakes the same models in at build time (`scripts/warm_ocr.py`). Running the tests in
the image with `docker run --network none` is the other offline route, but building the image in CI takes far
longer than restoring a 0.5 GB cache.

## PaddleOCR alone and the scan's net

Without Docling (an Intel Mac, or Apple silicon below macOS 14), PaddleOCR reads the scan's net line as
`Net total: 8 800.00`. Up to 959a983 the demo template read that as `8.0`, and the AI was asked (diagnosis cause
C2). The template now reads the last amount on the line, and the template-free layout parser reads it only when
the item rows add up to it, so both read `800.00`. `tests/test_template_free.py` checks this on PaddleOCR's
recorded text (`tests/fixtures/invoice-scan.paddleocr.json`); a PaddleOCR-only golden job is not added here.
