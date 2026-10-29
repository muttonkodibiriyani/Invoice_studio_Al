"""Check that the invoice readers are installed and read the sample invoices.

    .venv/bin/python scripts/check_engines.py            macOS / Linux
    .venv\\Scripts\\python scripts\\check_engines.py       Windows

Each reader runs the way an upload runs it (app.engines.local_read):
invoice2data on samples/invoice.pdf, PaddleOCR and Docling on the scanned
samples/invoice-scan.png. The first run downloads the OCR models, so it needs
the internet once and takes a few minutes.

With no arguments every reader is checked, and one that is not installed is
reported without failing the check. Name readers (invoice2data, paddleocr,
docling) to require them. After --any, name a group of which at least one must
work: a computer that reads scans with PaddleOCR is a working install even when
Docling cannot start, so the setup scripts ask for the scan readers that way.

A reader that installed but could not download its models on the first run is
reported separately from one that read the sample wrongly. The first is the
network, not the install, and the message says what to do about it.

The exit code is 0 when every required reader works and at least one of an --any
group does.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = {
    "invoice2data": ROOT / "samples" / "invoice.pdf",
    "paddleocr": ROOT / "samples" / "invoice-scan.png",
    "docling": ROOT / "samples" / "invoice-scan.png",
}
NAMES = {"invoice2data": "invoice2data", "paddleocr": "PaddleOCR", "docling": "Docling"}
# Both samples carry this synthetic invoice (scripts/warm_ocr.py checks the same).
NUMBER, LINES = "DEMO-2026-001", 2
# The reader called directly, to show its own error: local_read keeps only a generic hint.
DIRECT = {
    "invoice2data": "ocr_worker.digital(path)",
    "paddleocr": "ocr_worker.paddle(path, 'en')",
    "docling": "ocr_worker.docling(path)",
}
INSTALL = "./scripts/setup.sh --ocr (Windows: scripts\\setup.cmd --ocr)"
# A reader downloads its models on the first run. When the network refuses that, the reader
# raises from its download code, not from anything to do with the document, and the hint the
# app shows an operator ("this picture could not be read") is misleading here.
BLOCKED = ("LocalEntryNotFoundError", "OfflineModeIsEnabled", "snapshot_download",
           "huggingface_hub.errors", "ConnectError", "ConnectionError", "ProxyError",
           "HTTPSConnectionPool", "Max retries exceeded", "SSLCertVerificationError",
           "CERTIFICATE_VERIFY_FAILED", "Temporary failure in name resolution",
           "Name or service not known", "getaddrinfo failed", "Failed to download",
           # Windows: forbidden by access permissions, timed out, refused.
           "WinError 10013", "WinError 10060", "WinError 10061")


def could_not_download(lines):
    """True when the reader's own error is about fetching its models, not about the document."""
    text = "\n".join(lines)
    return any(sign in text for sign in BLOCKED)


def why_blocked(name, engine):
    """What to tell someone whose reader installed but could not fetch its models."""
    return [f"      {name} is installed, but the network refused its model download.",
            "      Nothing is wrong with the install or with the sample invoice, and the other",
            "      readers are unaffected.",
            "        - A company network or VPN usually blocks the model hosts. Try another network,",
            "          or name your proxy, then run the check again. No reinstall is needed:",
            "              set HTTPS_PROXY=http://proxy.example.com:8080     (Windows)",
            f"              .venv\\Scripts\\python scripts\\check_engines.py --any {engine}",
            "              export HTTPS_PROXY=http://proxy.example.com:8080  (macOS, Linux)",
            f"              .venv/bin/python scripts/check_engines.py --any {engine}",
            "        - The models are downloaded once. After that this reader works offline."]


def read(engine, data):
    """(works, what it read, whether the reader process failed, the error class it raised)"""
    from app import engines

    try:
        result = engines.local_read(engine, SAMPLES[engine], data)
    except ValueError as error:
        return False, str(error), True, getattr(error, "kind", "")
    text = (result.get("text") or "").strip()
    invoice = result.get("invoice") or {}
    if not text:
        return False, "read no text from the sample", False, ""
    if invoice.get("number") != NUMBER or len(invoice.get("lines") or []) != LINES:
        return False, (f"read {len(text)} characters but not the sample's invoice number and "
                       f"{LINES} lines"), False, ""
    return True, f"read {len(text)} characters: invoice {NUMBER}, {LINES} lines", False, ""


def direct_error(engine):
    """The last lines of the reader's own error when called directly, or [] when it works that way."""
    code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            f"from app import ocr_worker; path = Path(sys.argv[2]); {DIRECT[engine]}")
    result = subprocess.run([sys.executable, "-c", code, str(ROOT), str(SAMPLES[engine])], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if not result.returncode:
        return []
    return [line for line in result.stderr.splitlines() if line.strip() and "Warning" not in line][-12:]


def split(args):
    """(readers that must work, readers of which at least one must work)."""
    required, any_of, bucket = [], [], None
    for arg in args:
        if arg == "--any":
            bucket = any_of
            continue
        (required if bucket is None else bucket).append(arg)
    return required, any_of


def main(argv=None):
    required, any_of = split(list(sys.argv[1:] if argv is None else argv))
    wanted = required + any_of
    unknown = [name for name in wanted if name not in SAMPLES]
    if unknown:
        print(f"Unknown reader {', '.join(unknown)}; choose from {', '.join(SAMPLES)}.", file=sys.stderr)
        return 2
    from app import engines

    installed = {item["id"]: item for item in engines.capabilities()}
    # The first run downloads model weights inside the reader's time budget.
    os.environ.setdefault("INV_ENGINE_TIMEOUT", "1800")
    print("Invoice readers:", flush=True)
    # A reader takes a minute or two: in a terminal each one is announced before it starts and the
    # line is then rewritten with the result, so a long wait is never a silent one.
    live = sys.stdout.isatty()
    works_by_name, blocked, missing = {}, [], []
    with tempfile.TemporaryDirectory(prefix="inv-studio-check-") as directory:
        data = Path(directory)
        (data / "work").mkdir()
        (data / "templates").mkdir()
        for engine in wanted or list(SAMPLES):
            name = NAMES[engine]
            if not installed[engine]["installed"]:
                print(f"  {name:<13} NOT INSTALLED   add it with {INSTALL}", flush=True)
                works_by_name[name] = False
                if engine not in wanted:
                    missing.append(name)
                continue
            # Reading the sample takes a minute or two, and the first run downloads the models:
            # name the reader before it starts, so a long wait is not a silent one.
            pending = f"  {name:<13} reading the sample\u2026 (the first run downloads its models)"
            if live:
                print(pending, end="", flush=True)
            start = time.monotonic()
            works, detail, crashed, kind = read(engine, data)
            seconds = round(time.monotonic() - start)
            version = installed[engine]["version"]
            # The reader's own exception class already says a download was refused. Asking the reader
            # again would repeat the refused download, so it is only asked when the class says nothing.
            no_models = not works and engines.is_download_failure(kind)
            lines = direct_error(engine) if crashed and not no_models else []
            no_models = no_models or (not works and could_not_download(lines))
            status = "OK" if works else ("NO MODELS" if no_models else "FAILED")
            if no_models:
                detail = "installed, but could not download its models"
                blocked.append(name)
            done = f"  {name:<13} {status:<15} {detail} ({version}, {seconds} s)"
            # In a terminal the result rewrites the line the reader was announced on (the padding
            # clears what was there). Redirected to a file, only the result is written.
            print("\r" + done.ljust(len(pending)) if live else done, flush=True)
            works_by_name[name] = works
            if no_models:
                print("\n".join(why_blocked(name, engine)))
            if lines:
                print("      Its own error when called directly:")
                print("\n".join("        " + line for line in lines))
            elif crashed and not no_models:
                print("      It works when called directly, so the app's reader process is what fails "
                      "(its environment).")

    def broken(engines_):
        return [NAMES[engine] for engine in engines_ if not works_by_name.get(NAMES[engine])]

    # Named readers must work. An --any group only has to leave one working reader behind:
    # scans read fine on PaddleOCR alone, so Docling being stuck is not a failed install.
    stopped = broken(required) if wanted else [name for name, ok in works_by_name.items() if not ok]
    if any_of and len(broken(any_of)) == len(any_of):
        stopped += broken(any_of)
    if stopped:
        print(f"Not working: {', '.join(dict.fromkeys(stopped))}.")
        return 1
    ready = [name for name, ok in works_by_name.items() if ok]
    print("Readers that work: " + (", ".join(ready) if ready else "none") + "."
          + (f" Could not download their models: {', '.join(blocked)}. Every document is still read "
             f"by the readers above; add these later with the check command printed for them, "
             f"without reinstalling anything." if blocked else "")
          + (f" Not installed: {', '.join(missing)}." if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
