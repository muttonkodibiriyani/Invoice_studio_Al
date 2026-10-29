@echo off
rem Start Invoice Studio locally on Windows (Command Prompt). No sign-in is needed.
rem
rem     scripts\run.cmd
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\uvicorn.exe" (
  echo Invoice Studio is not installed yet. Run scripts\setup.cmd first.
  exit /b 1
)

rem Load .env, the same variable list the cloud deployment uses. A variable
rem already set in this Command Prompt wins over the file.
if exist ".env" (
  for /f "eol=# tokens=1* delims==" %%a in (.env) do (
    if not "%%~b"=="" if not defined %%a set "%%a=%%~b"
  )
)
if not defined INV_STUDIO_PORT set "INV_STUDIO_PORT=8765"
rem Windows Python otherwise reads and writes text files in the ANSI code page;
rem the app's files and its readers' output are UTF-8.
set "PYTHONUTF8=1"

echo Open http://127.0.0.1:%INV_STUDIO_PORT%  (press Ctrl+C to stop)
".venv\Scripts\uvicorn.exe" app.main:app --host 127.0.0.1 --port %INV_STUDIO_PORT% --no-access-log
exit /b %errorlevel%
