#!/usr/bin/env bash
# Waits until a Container Apps revision is Healthy and answers /health with 200 on its own address.
# Used by .github/workflows/deploy-azure.yml for the candidate and for the -live revision.
#
#   scripts/azure_revision_ready.sh <app> <resource group> <revision>
#
# A revision answers on its own address before it has traffic (INV_STUDIO_ALLOWED_HOSTS lets it). An
# internal environment is not reachable from a GitHub-hosted runner: there this needs the in-network
# runner of docs/CI_SECURITY.md.
set -euo pipefail
app=$1 rg=$2 revision=$3

# The template sets port 8080 and the /health probes only once it has run with a real image. An app still
# on the placeholder (port 80, no probes) never makes the real image healthy.
port=$(az containerapp show -n "$app" -g "$rg" --query "properties.configuration.ingress.targetPort" -o tsv)
if [ "$port" != "8080" ]; then
  echo "The app's ingress port is ${port:-unset}, not 8080: it is still set up for the placeholder image." >&2
  echo "Put the first real image on with scripts/azure_deploy.py --image (docs/AZURE.md section 5) first." >&2
  exit 1
fi

state=Unknown
for _ in $(seq 1 60); do
  state=$(az containerapp revision show -n "$app" -g "$rg" --revision "$revision" \
    --query "properties.healthState" -o tsv 2>/dev/null || echo Unknown)
  echo "$revision health: $state"
  [ "$state" = "Healthy" ] && break
  [ "$state" = "Unhealthy" ] && { echo "$revision is Unhealthy." >&2; exit 1; }
  sleep 20
done
[ "$state" = "Healthy" ] || { echo "$revision did not become healthy in 20 minutes." >&2; exit 1; }

fqdn=$(az containerapp revision show -n "$app" -g "$rg" --revision "$revision" --query "properties.fqdn" -o tsv)
for _ in $(seq 1 10); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$fqdn/health" || echo 000)
  echo "https://$fqdn/health: $code"
  [ "$code" = "200" ] && exit 0
  sleep 15
done
echo "$revision did not answer /health with 200." >&2
exit 1
