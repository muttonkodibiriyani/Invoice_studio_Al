#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Load .env, the same variable list the cloud deployment uses. A variable
# already set in the environment wins, so INV_STUDIO_DATA=/tmp/x ./scripts/run.sh
# still overrides the file.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}
    key=${key#export }
    key=${key%% *}
    value=${line#*=}
    case "$key" in
      *[!A-Z0-9_]*|'') continue ;;
    esac
    # Surrounding quotes are stripped.
    case "$value" in
      \"*\") value=${value#\"}; value=${value%\"} ;;
      \'*\') value=${value#\'}; value=${value%\'} ;;
    esac
    [ -n "$value" ] || continue
    [ -n "${!key:-}" ] || export "$key=$value"
  done < .env
fi

exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${INV_STUDIO_PORT:-8765}" --no-access-log
