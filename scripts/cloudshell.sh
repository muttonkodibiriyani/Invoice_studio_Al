#!/usr/bin/env bash
# Invoice Studio on Azure from Azure Cloud Shell (Bash): every deploy step is one command.
#
#   ./scripts/cloudshell.sh preflight      deploy-day checklist: sign-in, code is merged main, kit check; creates nothing
#   ./scripts/cloudshell.sh check          preflight only; creates nothing
#   ./scripts/cloudshell.sh infra          create every resource (asks for the two secrets, hidden; type DEPLOY)
#   ./scripts/cloudshell.sh vault-access   give yourself Key Vault Secrets Officer (AZURE.md §3)
#   ./scripts/cloudshell.sh build          build the image inside ACR (40-90 minutes the first time)
#   ./scripts/cloudshell.sh image          put the image `build` made on the app (first image only; type DEPLOY)
#   ./scripts/cloudshell.sh db-role        move the app off the database admin login (AZURE.md §5a, interactive)
#   ./scripts/cloudshell.sh lockdown       close the public endpoints of Key Vault and Blob, and prove the app
#   ./scripts/cloudshell.sh test           the Postman smoke, security and invoice-flow checks, with newman
#   ./scripts/cloudshell.sh smoke          no-token checks on the live URL: health, sign-in required, security headers
#   ./scripts/cloudshell.sh live-run DIR   real invoices end to end through the app, AI on (scripts/live_invoice_run.py)
#   ./scripts/cloudshell.sh status         names, revision health, lockdown state, alerts
#
# The order for a first deploy is the order above; docs/DEPLOY_CLOUD_SHELL.md explains each step and what to do if
# one stops. The deploy kit is ~/inv-studio-azure/deploy-kit.env (KIT=... to use another).
set -euo pipefail
cd "$(dirname "$0")/.."

KIT=${KIT:-$HOME/inv-studio-azure/deploy-kit.env}
STATE=$HOME/inv-studio-azure
OUTPUTS=$STATE/outputs.env

need_kit() {
  if [ ! -f "$KIT" ]; then
    echo "No deploy kit at $KIT. Create it from the example and fill it in:" >&2
    echo "  mkdir -p ~/inv-studio-azure && chmod 700 ~/inv-studio-azure" >&2
    echo "  cp infra/azure/deploy-kit.example.env $KIT && chmod 600 $KIT && nano $KIT" >&2
    exit 1
  fi
}

need_outputs() {
  if [ ! -f "$OUTPUTS" ]; then
    echo "No $OUTPUTS: run ./scripts/cloudshell.sh infra first (it saves the names it made)." >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$OUTPUTS"
}

step=${1:-help}
case "$step" in
  preflight)
    # Stops at the first failed item; every item is read-only.
    ok() { echo "  ok    $*"; }
    no() { echo "  STOP  $*" >&2; exit 1; }
    echo "Deploy-day preflight"
    need_kit
    want=$(sed -n 's/^AZ_SUBSCRIPTION_ID=\([^ #]*\).*/\1/p' "$KIT")
    sub=$(az account show --query id -o tsv 2>/dev/null) || no "not signed in: az login"
    [ "$sub" = "$want" ] && ok "signed in to $sub" || no "signed in to $sub, the kit says $want: az account set -s $want"
    [ -z "$(git status --porcelain)" ] && ok "checkout clean" || no "checkout has local changes: git status"
    git fetch -q origin main
    [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && ok "HEAD is origin/main $(git rev-parse --short HEAD)" \
      || no "HEAD $(git rev-parse --short HEAD) is not origin/main $(git rev-parse --short origin/main): git checkout main && git pull"
    if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
      ci=$(gh run list --workflow CI --commit "$(git rev-parse HEAD)" --json conclusion -q '.[0].conclusion' 2>/dev/null || true)
      [ "$ci" = "success" ] && ok "CI passed on this commit" || no "CI on this commit: ${ci:-no run found}; deploy only a commit CI passed"
    else
      echo "  check CI passed on $(git rev-parse --short HEAD) in GitHub Actions (gh not signed in here)"
    fi
    if [ -f "$OUTPUTS" ]; then
      # shellcheck disable=SC1090
      source "$OUTPUTS"
      ok "existing deployment $APP in $RG; this deploy is a new image (Actions -> Deploy to Azure, AZURE.md §8)"
      curl -fsS -o /dev/null --max-time 20 "$APP_URL/health" && ok "live app answers /health" || echo "  WARN  $APP_URL/health does not answer 200"
    else
      ok "first deploy: next steps are infra, vault-access, build, image (docs/DEPLOY_CLOUD_SHELL.md)"
      python3 scripts/azure_deploy.py --kit "$KIT" --check
    fi
    echo "Preflight passed."
    ;;
  check)
    need_kit
    python3 scripts/azure_deploy.py --kit "$KIT" --check
    ;;
  infra)
    need_kit
    python3 scripts/azure_deploy.py --kit "$KIT"
    ;;
  vault-access)
    need_outputs
    me=$(az ad signed-in-user show --query id -o tsv)
    scope=$(az keyvault show -n "$VAULT" -g "$RG" --query id -o tsv)
    az role assignment create --assignee-object-id "$me" --assignee-principal-type User \
      --role "Key Vault Secrets Officer" --scope "$scope" -o none
    echo "Key Vault Secrets Officer on $VAULT for you. It can take a few minutes to apply."
    ;;
  build)
    need_outputs
    tag=$(git rev-parse --short HEAD)
    az acr build --registry "$ACR" -g "$RG" --platform linux/amd64 --file Dockerfile \
      --image "inv-studio:$tag" --timeout 14400 .
    server=$(az acr show -n "$ACR" --query loginServer -o tsv)
    digest=$(az acr repository show -n "$ACR" --image "inv-studio:$tag" --query digest -o tsv)
    echo "$server/inv-studio@$digest" > "$STATE/last-image"
    echo "Built $server/inv-studio@$digest (saved in $STATE/last-image)."
    ;;
  image)
    need_kit
    need_outputs
    image=${2:-$(cat "$STATE/last-image" 2>/dev/null || true)}
    if [ -z "$image" ]; then
      echo "No image: run ./scripts/cloudshell.sh build first, or pass <server>/inv-studio@sha256:<digest>." >&2
      exit 1
    fi
    python3 scripts/azure_deploy.py --kit "$KIT" --image "$image"
    scripts/azure_revision_ready.sh "$APP" "$RG" \
      "$(az containerapp revision list -n "$APP" -g "$RG" --query "sort_by([], &properties.createdTime)[-1].name" -o tsv)"
    ;;
  db-role)
    need_outputs
    echo "Inside the container, run:  python scripts/azure_db_app_role.py create   then  exit"
    echo "Then point the app at the new role as docs/AZURE.md §5a step 3 shows."
    az containerapp exec -n "$APP" -g "$RG" --command bash
    ;;
  lockdown)
    need_outputs
    python3 scripts/azure_lockdown.py -g "$RG" "${@:2}"
    ;;
  test)
    need_outputs
    if [ -z "${TOKEN:-}" ]; then
      echo "Paste an admin token (docs/API.md §2: browser developer tools, Authorization header after Bearer)."
      read -rs -p "Token: " TOKEN && echo
    fi
    member_args=()
    if [ -n "${MEMBER_TOKEN:-}" ]; then member_args=(--env-var "memberToken=$MEMBER_TOKEN"); fi
    npx --yes newman@6 run docs/api/invoice-studio.postman_collection.json \
      -e docs/api/invoice-studio.postman_environment.json --working-dir . \
      --env-var "baseUrl=$APP_URL" --env-var "token=$TOKEN" "${member_args[@]}" \
      --folder "1. Smoke test" --folder "2. Security checks" --folder "3. Invoice flow"
    ;;
  smoke)
    need_outputs
    url=${2:-$APP_URL}
    fail=0
    code() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$url$1" || true; }
    expect() {  # path, wanted status
      got=$(code "$1")
      if [ "$got" = "$2" ]; then echo "  ok    $1 $got"; else echo "  FAIL  $1 $got, want $2"; fail=1; fi
    }
    echo "Smoke test of $url (no token)"
    expect /health 200
    expect /api/public-config 200
    expect /api/session 401
    expect /api/state 401
    headers=$(curl -s -D - -o /dev/null --max-time 30 "$url/" || true)
    for h in Strict-Transport-Security Content-Security-Policy X-Content-Type-Options; do
      if grep -qi "^$h:" <<<"$headers"; then echo "  ok    header $h"; else echo "  FAIL  header $h missing"; fail=1; fi
    done
    curl -fsS --max-time 30 "$url/api/public-config" | grep -q '"cloud": *true' && echo "  ok    cloud sign-in on" \
      || { echo "  FAIL  /api/public-config does not say cloud sign-in is on"; fail=1; }
    [ $fail = 0 ] && echo "Smoke passed. Signed-in checks: ./scripts/cloudshell.sh test" || { echo "Smoke FAILED." >&2; exit 1; }
    ;;
  live-run)
    need_outputs
    dir=${2:?usage: ./scripts/cloudshell.sh live-run DIR  (the real invoices; they never go into git)}
    if [ -z "${INV_STUDIO_TOKEN:-}" ]; then
      echo "Paste a member or admin token (docs/API.md §2: browser developer tools, Authorization header after Bearer)."
      read -rs -p "Token: " INV_STUDIO_TOKEN && echo
      export INV_STUDIO_TOKEN
    fi
    [ -x "$STATE/venv/bin/python" ] || python3 -m venv "$STATE/venv"
    "$STATE/venv/bin/pip" install -q httpx "pydantic>=2"
    out=$STATE/live-run-$(date -u +%Y%m%dT%H%M%SZ)
    "$STATE/venv/bin/python" scripts/live_invoice_run.py --url "$APP_URL" --out "$out" "$dir" "${@:3}"
    echo "Report in $out (statuses, counts and issue codes only; no invoice values)."
    ;;
  status)
    need_outputs
    echo "Resource group $RG, app $APP at $APP_URL"
    az containerapp revision list -n "$APP" -g "$RG" -o table \
      --query "[?properties.active].{revision:name,health:properties.healthState,traffic:properties.trafficWeight}"
    python3 scripts/azure_lockdown.py -g "$RG" --status
    az monitor metrics alert list -g "$RG" -o table --query "[].{alert:name,enabled:enabled,severity:severity}"
    curl -fsS "$APP_URL/health" && echo
    ;;
  *)
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
