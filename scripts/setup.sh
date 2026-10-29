#!/usr/bin/env bash
# One-command local install for macOS and Linux. No sign-in, no cloud account.
#
#   git clone <repo> && cd inv-studio && ./scripts/setup.sh && ./scripts/run.sh
#
# Creates .venv, installs the application and its tests, and writes a .env from
# .env.example. The same variable names configure the cloud deployment; see
# .env.example.
#
#   ./scripts/setup.sh --ocr
#
# also installs the scan readers for scanned PDFs and photos, PaddleOCR and
# Docling (scripts/install_ocr.py), and checks every reader on the samples.
set -euo pipefail
cd "$(dirname "$0")/.."

ocr=0
for arg in "$@"; do
  case "$arg" in
    --ocr) ocr=1 ;;
    *) echo "Unknown option: $arg. The one option is --ocr (also install the scan readers)." >&2; exit 2 ;;
  esac
done

if command -v python3.12 >/dev/null 2>&1; then
  python=python3.12
elif command -v python3 >/dev/null 2>&1; then
  python=python3
else
  echo "Python 3.12 is required. Install it from https://www.python.org/downloads/ (macOS: brew install python@3.12)." >&2
  exit 1
fi

version=$("$python" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
case "$version" in
  3.11|3.12|3.13) ;;
  *) echo "Python 3.12 is the tested runtime; found $version via $python. Install Python 3.12 and run this again." >&2; exit 1 ;;
esac

# uv is faster when it is already installed; the stdlib path keeps the install
# to one command on a machine that has only Python.
# Older Macs get no ready-made build of the newest pypdfium2 (5.12+ needs
# macOS 13) or cryptography (49+ has no Intel Mac build), and building them
# from source fails there. --only-binary installs the newest release that has a
# build for this machine instead; the ranges in pyproject.toml allow it.
installer=
if command -v uv >/dev/null 2>&1; then
  installer=--uv
  echo "==> Creating .venv with uv ($version)"
  # --allow-existing lets setup run again, e.g. after a git pull.
  uv venv --allow-existing --python "$python"
  echo "==> Installing Invoice Studio"
  VIRTUAL_ENV=.venv uv pip install -e '.[test]' --only-binary pypdfium2 --only-binary cryptography
else
  # Debian and Ubuntu ship Python without the venv bootstrap, and "python -m venv"
  # then fails deep in ensurepip. Say which package is missing instead.
  if ! "$python" -c "import ensurepip" >/dev/null 2>&1; then
    echo "$python cannot create a virtual environment: the venv bootstrap is missing." >&2
    echo "Install it with one of:" >&2
    echo "    sudo apt install python${version}-venv        # Debian or Ubuntu" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh   # uv, needs no root" >&2
    echo "Then run ./scripts/setup.sh again. macOS and Windows installers already include it." >&2
    exit 1
  fi
  echo "==> Creating .venv ($version)"
  "$python" -m venv .venv
  echo "==> Installing Invoice Studio (this takes a few minutes the first time)"
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e '.[test]' --only-binary pypdfium2 --only-binary cryptography
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Wrote .env from .env.example. It is ignored by git; leave it blank to run locally."
fi

# The application itself is already installed by here, so a scan reader that will not
# start is a smaller problem than it used to be: say how to start the app either way, and
# only report a failure when no scan reader works at all.
scan_readers=0
if [ "$ocr" = 1 ] && ! .venv/bin/python scripts/install_ocr.py $installer; then
  scan_readers=1
fi

echo
echo "Installed. Start the app with:"
echo "    ./scripts/run.sh"
echo "Then open http://127.0.0.1:${INV_STUDIO_PORT:-8765} — no sign-in is needed locally."
echo "Connect an AI reader (OpenAI, Anthropic, ChatGPT, Claude Code, Vertex AI) under Engines & AI."
if [ "$ocr" = 0 ]; then
  echo "Scanned PDFs and photos need the scan readers: ./scripts/setup.sh --ocr"
fi
if [ "$scan_readers" = 1 ]; then
  echo >&2
  echo "No scan reader works yet, so a scanned PDF or photo will read as \"extraction failed\"." >&2
  echo "Digital PDFs are unaffected, and the packages are installed: nothing needs reinstalling." >&2
  echo "The messages above say what stopped them. When it is fixed, re-run only the check:" >&2
  echo "    .venv/bin/python scripts/check_engines.py --any paddleocr docling" >&2
  exit 1
fi
