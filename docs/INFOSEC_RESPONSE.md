# InfoSec review: what changed, and how to check it

Answers the open points of the low-level design (*Invoice Studio on Azure: low-level design for InfoSec*, §8 to §10).
Each row names the change in this repository and a command that shows it on the deployed resources. The deploy
order is [DEPLOY_CLOUD_SHELL.md](DEPLOY_CLOUD_SHELL.md); the API and its tests are [API.md](API.md).

## Open points, closed

| LLD question | Before | Now | Where |
|---|---|---|---|
| Is outbound traffic restricted? | Azure's default: open | The apps NSG allows 443 to `AzureActiveDirectory`, `MicrosoftContainerRegistry`, `AzureFrontDoor.FirstParty`, `AzureContainerRegistry`, `Storage`, `AzureKeyVault`, `AzureMonitor`, plus 443 and 5432 to the private endpoints, then **denies `Internet`**. Optional route of all outbound traffic through the hub firewall (`HUB_FIREWALL_IP`). | `infra/azure/network.bicep` (`restrictEgress`, `egressFirewallIp`) |
| Are Key Vault and Blob private? | Public endpoints behind RBAC | **Private endpoints** in the endpoints subnet with private DNS; public endpoints **switched off** by `scripts/azure_lockdown.py`, which proves the app still works and reverts by itself if not. A re-run of the template keeps them off. | `infra/azure/privatelink.bicep`, `storage.bicep`, `keyvault.bicep`, `scripts/azure_lockdown.py` |
| Does the AI call use a managed identity? | api-key from Key Vault | `INV_STUDIO_AZURE_AUTH=managed_identity`: an Entra token for the app's user-assigned identity, no key. The kit line `CITADEL_AUTH_MODE` sets it. | `app/azure_foundry.py`, `containerapp.bicep` (`foundryAuthMode`), AZURE.md §7 |
| Can an admin connect a non-Azure AI provider? | Yes | **No.** `INV_STUDIO_AI_PROVIDERS=azure`: connecting, listing models of, or reading with any other provider answers 403, and the engine refuses before any call. The NSG blocks those hosts as well. | `app/providers.py`, `app/main.py`, `containerapp.bicep` (`aiProviders`) |
| Is there a WAF in front? | No | Decision for the platform. Two options are ready: the **internal environment** (`internalEnvironment`, the target design: no public address, entry through the hub's WAF), or **client IP ranges at the ingress** (`INGRESS_ALLOWED_CIDRS`, everything else gets 403). | `containerapp.bicep` (`ingressAllowedCidrs`), `main.bicep` (`internalEnvironment`) |
| Is the app highly available? | One replica, one zone | Unchanged by design for the current volume (LLD §8). The "Now" phase is done: **alerts** on container restarts, more than 5 server errors in 5 minutes, database CPU above 80% and database storage above 85%, mailed to `ALERT_EMAILS`. | `infra/azure/alerts.bicep` |
| Attack pack F2: plan not tied to its maker | Anyone with the plan token | A processing plan records the account that made it; confirm, renew and upload refuse any other account (409). | `app/main.py` `require_plan`, `tests/test_plan_owner.py` |

Already closed before this change, and unchanged: 70+ API routes need a valid Entra token and role (deny by
default), the database has no public endpoint, no storage keys or registry passwords exist, uploaded files are read in
separate processes with no keys, F1 (upload check capped and time-limited).

## What InfoSec can verify (LLD §10), with the command

Run after `source ~/inv-studio-azure/outputs.env` in Cloud Shell.

| Check | Command | Expect |
|---|---|---|
| NSGs attached, outbound denied | `az network nsg rule list -g "$RG" --nsg-name "${APP%-api}-vnet-apps-nsg" -o table` | `allow-out-*` rules on 443, `deny-out-internet` at 4000 |
| No token: 401; no role: 403 | `curl -s -o /dev/null -w '%{http_code}\n' "$APP_URL/api/state"` and the Postman *2. Security checks* folder | `401`; the folder all ticks |
| Database not reachable from outside | `az postgres flexible-server show -g "$RG" -n "$DB_SERVER" --query network.publicNetworkAccess` | `"Disabled"` |
| Storage: no shared key, no anonymous, private | `az storage account show -g "$RG" -n "$STORAGE" --query "{sharedKey:allowSharedKeyAccess,anonymous:allowBlobPublicAccess,public:publicNetworkAccess}"` | `false`, `false`, `"Disabled"` |
| Key Vault: RBAC, soft delete, purge protection, private | `az keyvault show -n "$VAULT" --query "{rbac:properties.enableRbacAuthorization,softDelete:properties.enableSoftDelete,purge:properties.enablePurgeProtection,public:properties.publicNetworkAccess}"` | `true`, `true`, `true`, `"Disabled"` |
| Private endpoints approved | `az network private-endpoint list -g "$RG" --query "[].{name:name,state:privateLinkServiceConnections[0].privateLinkServiceConnectionState.status}" -o table` | the database, vault and blob endpoints, all `Approved` |
| Security headers | `curl -sI "$APP_URL/health"` | `content-security-policy`, `strict-transport-security`, `x-content-type-options: nosniff`, `cache-control: no-store` |
| Only Azure AI | Postman *A non-Azure AI provider is refused (403)* | 403 |
| Alerts exist | `az monitor metrics alert list -g "$RG" -o table` | four alerts, enabled |
| App reaches database and Blob privately | `./scripts/cloudshell.sh lockdown` (safe to repeat) or Postman *Readiness of database and storage* | `ok` for both |

Recovery targets for the single-replica setup still need the business owner's agreement (LLD §7, §10).

## Proven before release

- `tests/test_infosec_hardening.py`: the template rules above, managed identity (no key sent, a mounted key
  ignored, a token failure that leaks no address), the provider limit on every route, the readiness route.
- `tests/test_azure_lockdown.py`: nothing is closed without approved endpoints and DNS; an unhealthy app or a
  failed readiness check opens everything again.
- `tests/test_azure_deploy.py`, `tests/test_azure_infra.py`: the kit's new lines, and a re-run keeping a
  locked-down vault private.
- `tests/test_api_docs.py`: the API documentation lists exactly the routes the app serves, at the levels it enforces.
- The Postman collection's smoke, security and invoice-flow folders: 78 of 78 checks passed with newman against
  the app served over HTTP in cloud mode, with a stand-in Entra tenant.
- `main.bicep` compiles and lints clean with Bicep 0.48.1, the version CI uses.

Not provable before a real deploy: that the NSG service tags let the platform pull its system images in your
region, that Key Vault references resolve through the private endpoint, and that the hub DNS answers the
privatelink zones. The first `infra`, `image` and `lockdown` runs prove each, and `lockdown` reverts if the second
or third does not hold.
