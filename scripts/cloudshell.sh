#!/usr/bin/env bash
# Invoice Studio on Azure from Azure Cloud Shell (Bash): every deploy step is one command.
#
#   ./scripts/cloudshell.sh check          preflight only; creates nothing
#   ./scripts/cloudshell.sh infra          create every resource (asks for the two secrets, hidden; type DEPLOY)
#   ./scripts/cloudshell.sh vault-access   give yourself Key Vault Secrets Officer (AZURE.md §3)
#   ./scripts/cloudshell.sh build          build the image inside ACR (40-90 minutes the first time)
#   ./scripts/cloudshell.sh image          put the image `build` made on the app (first image only; type DEPLOY)
#   ./scripts/cloudshell.sh db-role        move the app off the database admin login (AZURE.md §5a, interactive)
#   ./scripts/cloudshell.sh lockdown       close the public endpoints of Key Vault and Blob, and prove the app
#   ./scripts/cloudshell.sh test           the Postman smoke, security and invoice-flow checks, with newman
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
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
