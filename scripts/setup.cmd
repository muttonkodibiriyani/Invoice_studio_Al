@echo off
rem One-command local install for Windows (Command Prompt). No sign-in, no cloud
rem account.
rem
rem     git clone <repo>
rem     cd inv-studio
rem     scripts\setup.cmd
rem     scripts\run.cmd
rem
rem Creates .venv, installs the application and its tests, and writes a .env from
rem .env.example. The same variable names configure the cloud deployment; see
rem .env.example.
rem
rem     scripts\setup.cmd --ocr
rem
rem also installs the scan readers for scanned PDFs and photos, PaddleOCR and
rem Docling (scripts\install_ocr.py), and checks every reader on the samples.
setlocal
cd /d "%~dp0.."

set "OCR="
if /i "%~1"=="--ocr" set "OCR=1"
if not "%~1"=="" if not defined OCR (
  echo Unknown option: %~1. The one option is --ocr ^(also install the scan readers^).
  exit /b 2
)

set "PYEXE="
py -3.12 -c "import sys" >nul 2>&1 && set "PYEXE=py -3.12"
if not defined PYEXE (
  python -c "import sys;raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13)) else 1)" >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
  echo Python 3.12 is required and was not found.
  echo Install it from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH", then run this again.
  exit /b 1
)

echo ==^> Creating .venv
%PYEXE% -m venv .venv || exit /b 1
echo ==^> Installing Invoice Studio ^(this takes a few minutes the first time^)
".venv\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -e ".[test]" || exit /b 1

if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo ==^> Wrote .env from .env.example. It is ignored by git; leave it blank to run locally.
)

rem The application itself is already installed by here, so a scan reader that will not
rem start is a smaller problem than it used to be: say how to start the app either way, and
rem only report a failure when no scan reader works at all.
set "SCANREADERS="
if defined OCR (
  ".venv\Scripts\python.exe" scripts\install_ocr.py
  if errorlevel 1 set "SCANREADERS=failed"
)

echo.
echo Installed. Start the app with:
echo     scripts\run.cmd
if not defined INV_STUDIO_PORT set "INV_STUDIO_PORT=8765"
echo Then open http://127.0.0.1:%INV_STUDIO_PORT% - no sign-in is needed locally.
echo Connect an AI reader ^(OpenAI, Anthropic, ChatGPT, Claude Code, Vertex AI^) under Engines ^& AI.
if not defined OCR echo Scanned PDFs and photos need the scan readers: scripts\setup.cmd --ocr
if defined SCANREADERS (
  echo.
  echo No scan reader works yet, so a scanned PDF or photo will read as "extraction failed".
  echo Digital PDFs are unaffected, and the packages are installed: nothing needs reinstalling.
  echo The messages above say what stopped them. When it is fixed, re-run only the check:
  echo     .venv\Scripts\python scripts\check_engines.py --any paddleocr docling
  exit /b 1
)
exit /b 0
