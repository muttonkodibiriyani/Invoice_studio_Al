# Invoice Studio on Azure

## Azure, simply

The short version, for someone who is not a cloud specialist. The detailed steps follow from §0.

Azure is a third place to run Invoice Studio, next to a Windows or Mac computer and Google Cloud.
On Azure, people sign in with their work account in the team's Microsoft Entra ID tenant, and only
members of two Entra groups get in. The Google Cloud copy keeps its own Google sign-in. The two hosted
copies share no sign-in, no database and no files.

### The picture

```text
                          HTTPS
  Browser ───────────────────────────────► Container app  (the whole app: page, API, readers)
     │                                        │
     │ sign-in (work account)                 ├──► PostgreSQL     invoices, extracted data, workbooks
     ▼                                        │    (private network only)
  Microsoft Entra ID ◄──── public signing keys┤
  (admin and member groups)  (no credential)  ├──► Blob Storage   the uploaded invoice files
                                              │
                                              ├──► Key Vault      database login, encryption key, AI key
                                              │
                                              └──► AI provider    only when the local readers leave gaps
                                                   (Azure AI Foundry, or another the admin picks)

  The app's image comes from Container Registry. Logs from every part go to Log Analytics.
```

### What runs, and what each part costs

Sizes are the template defaults (`infra/azure/main.bicep`). Costs are per month, UAE North, from idle
to busy; the next table shows how they add up.

| Part | What it does | Needed? | Size at the defaults | Per month |
|---|---|---|---|---|
| Container app and its environment | Runs the app: the web page, the API and the invoice readers. Cloud Run on Google | Required | 4 vCPU, 8 GiB, one copy, always on | US$93–310 |
| PostgreSQL Flexible Server 16 | Keeps users, invoices, extracted data and the Excel workbooks. Cloud SQL on Google | Required | Standard_B2s (2 vCores, 4 GiB), 32 GB disk, 14 days of backups | US$62 |
| Private network, private endpoint, private DNS zone | Lets the app reach the database while the database has no internet address | Required | the IPAM /24, two subnets with NSGs, one endpoint | US$8 |
| Blob Storage | Keeps the uploaded invoice files, supplier templates and learned files. Cloud Storage on Google | Required | Standard LRS, Hot, container `evidence` | US$0–3 |
| Container Registry | Holds the app image (about 5.6 GB) and builds it | Required | Basic | US$5–8 |
| Key Vault | Keeps the three secrets: database login, the key that encrypts stored AI credentials, the Foundry key | Required | Standard | under US$1 |
| Log Analytics | Keeps app logs and the audit logs of vault, files and registry | Required | Pay per GB, 30 days kept | US$0–15 |
| Managed identity | The app's own login to the registry, vault and files. No password exists | Required | One identity | US$0 |
| Microsoft Entra ID | Sign-in: one app registration, two app roles granted to two groups. The app holds no Entra secret | Required | The team's workforce tenant | US$0 (group-to-role assignment needs Entra ID P1, see §6) |
| Azure AI Foundry | Reads what the local readers could not | Optional | In a separate subscription | Not on this bill |

### How one invoice moves

1. **Upload.** The browser sends the file (PDF or image, up to 12 MB) to the container app over HTTPS,
   with the user's Entra access token. The app checks the token against the tenant's public signing
   keys, fetched from Microsoft and cached for a day. No invoice content goes to Microsoft Entra.
2. **Store the file.** The file goes into Blob Storage (`evidence/uploads/`). A job row goes into
   PostgreSQL. A working copy sits on the container's own disk and is gone after a restart.
3. **Read it locally.** The readers in the container read the invoice: the PDF text layer, and
   PaddleOCR and Docling for scans and photos. Nothing leaves Azure in this step.
4. **AI, only for gaps.** If the local read leaves gaps, the document or its text goes to the AI
   provider an administrator set up. An administrator can also turn on an AI cross-check, which sends
   complete invoices too. It is off by default. This is the only step where invoice content leaves Azure.
5. **Save the result.** The extracted data, the checks and an audit entry go into PostgreSQL.
6. **Export.** The Excel workbook is built and stored in PostgreSQL, not in Blob Storage. The browser
   downloads it from there.
7. **Delete.** Deleting the invoice removes its job, its workbook and its file, and keeps a short
   record that it was deleted. The file can still be
   recovered for **up to about 15 days**: 7 days as an old version, up to a day for Azure's clean-up run,
   then 7 days in soft delete. A file uploaded more than 7 days before the delete goes in about 8 days.
   The database rows stay in the backups for 14 days. §2 has the details.

### What it costs per month

Azure retail prices API (`prices.azure.com/api/retail/prices`), region `uaenorth`, list prices in US
dollars, read on 28 Oct 2026. A month is 730 hours. Sizes are checked against `infra/azure/*.bicep` at
main `3878804`.

- **Idle**: the app is on, nobody uses it.
- **Typical**: in use 8 hours a day on 22 working days, idle the rest.
- **Busy**: reading invoices around the clock.

| Line item | Price | Idle | Typical | Busy |
|---|---|---|---|---|
| Container app, CPU | US$0.000024 per vCPU-second working, US$0.000003 idle; first 180,000 vCPU-seconds a month free | 31 | 84 | 248 |
| Container app, memory | US$0.000003 per GiB-second, working or idle; first 360,000 free | 62 | 62 | 62 |
| PostgreSQL B2s compute | US$0.079 an hour | 58 | 58 | 58 |
| PostgreSQL disk, 32 GB | US$0.138 per GB; backups free up to the disk size | 4 | 4 | 4 |
| Private endpoint and DNS zone | US$0.01 an hour, US$0.01 per GB through it; US$0.50 a zone | 8 | 8 | 8 |
| Blob Storage | US$0.0203 per GB, plus US$0.06 per 10,000 writes | 0 | 1 | 3 |
| Container Registry, Basic | US$0.1666 a day, 10 GB included, then US$0.10 per GB; builds US$0.0001 per vCPU-second after 6,000 free | 5 | 6 | 8 |
| Key Vault | US$0.0396 per 10,000 reads | 0 | 0 | 1 |
| Log Analytics | First 5 GB a month free, then US$3.29 per GB | 0 | 0 | 15 |
| Data out of Azure | First 100 GB a month free | 0 | 0 | 0 |
| **Total** | | **about US$168** | **about US$223** | **about US$407** |

The free amounts for the container app come from Microsoft's
[Container Apps billing page](https://learn.microsoft.com/en-us/azure/container-apps/billing), not the
price API. The blob, log and registry figures for typical and busy are estimates of volume, not
measurements.

Three things can move these numbers:

- **The idle rate needs a quiet app.** Azure charges the lower CPU rate only while the copy handles no
  request, uses under 0.01 vCPU and receives under 1,000 bytes a second. Nobody has measured whether the
  app gets that quiet. If it never does, idle costs the busy price: about US$310 for the container app.
- **Open: an environment fee of about US$104 a month.** The price API lists "Environment Management
  Hour" at US$0.143 an hour. Microsoft's billing page names a management charge only for a Dedicated
  workload profile, an environment private endpoint or planned maintenance.
  This template uses none of them. Check Azure Cost Management after the first day of running.
- **Each deploy runs a second copy for a while.** The tested new version runs next to the live one until
  traffic moves. That costs about US$0.13 an hour idle, US$0.43 working.

**Azure and Google Cloud side by side.** `docs/RUNBOOK.md` puts the Google Cloud copy at about US$7–8 a
day, about US$215–245 a month. Both together cost about **US$380–650 a month**, or US$105 more if the
environment fee applies.

### What the owner provides before the first deploy

- [ ] An Azure subscription and an empty resource group in one region (the example uses UAE North).
- [ ] Owner on that resource group, or Contributor plus User Access Administrator.
- [ ] Azure CLI 2.60 or newer with Bicep and the `containerapp` extension, `jq` and `python3`.
- [ ] From the Entra administrator (§6): the tenant id `<AZ_TENANT_ID>`, the app registration's client
      id `<ENTRA_APP_CLIENT_ID>`, and the admin and member groups `<ENTRA_GROUP_ADMIN>` and
      `<ENTRA_GROUP_MEMBER>` (display names `<ENTRA_GROUP_ADMIN_NAME>`, `<ENTRA_GROUP_MEMBER_NAME>`),
      with the right people in each.
- [ ] Someone who can add a redirect URI to that app registration after step 2 (`<ENTRA_REDIRECT_URI_OWNER>`).
- [ ] A new database password and a new encryption key, kept in a password manager (§1).
- [ ] If Foundry is used: its endpoint, deployment name and API key (§7).
- [ ] A choice: purge protection on the vault (production; it can never be turned off) or off (a trial
      to throw away).
- [ ] Only if the GitHub deploy workflow is used (§4): one Azure app registration for deploys, three
      secrets and seven variables (four of them the Entra values) in the repository, and a reviewer on
      the `azure` environment. Not needed for a deploy by hand from Cloud Shell.

### Could be simpler or cheaper

None of these is made in the template. The smaller database is one value in the parameter file. Turning
the copy off is one command, and the next template run turns it back on.

| Option | Saving per month | What it gives up |
|---|---|---|
| Smaller database: `postgresSkuName` `Standard_B1ms` (1 vCore, 2 GiB, US$0.02 an hour) | about US$43 | Half the database CPU and memory. Not measured against real use |
| Turn the Azure copy off while nobody uses it: `--min-replicas 0` | up to US$93 when idle | The first visit waits for the 5.6 GB image and the reader warm-up, up to about 7 minutes. An invoice in progress waits for the next start |
| Smaller container: `cpuCores` `2.0`, `memory` `4Gi`, with an image that leaves out PaddleOCR and Docling | about US$47 idle, US$157 busy | Scans and photos are no longer read locally, so they go to the AI provider. That costs AI fees and sends more invoice content out of Azure. Not a cost decision alone: it needs its own data-flow approval. The image has no such option today, and 4 GiB is untested |
| Run one hosted copy only, once users have moved | about US$215–245 (Google) or the Azure total | The second copy. A move of the data is not written yet (§13) |
| Drop the private network and give the database a public address | about US$8 | The database would face the internet. Not recommended |

The detailed reference starts here. Phase 1 of the Azure move runs the same container image as Cloud
Run, with each Google service swapped for its Azure equivalent. The Azure deployment holds no Google
credential and no Entra secret: sign-in is Microsoft Entra ID, and who gets in, with which role, is
decided in Entra, not in the app (§6).
Everything is in `infra/azure/*.bicep` and `.github/workflows/deploy-azure.yml`. The whole run below
takes about an hour, most of it waiting for the first image build.

## 0. Before you start

You need:

- **An Azure subscription and an empty resource group** in one region. UAE North and West Europe both
  have every service used here. One region for everything keeps egress charges out of the picture.
- **Owner, or Contributor + User Access Administrator, on that resource group.** The template creates
  three role assignments (the app identity's access to the registry, the vault and the storage
  account), and plain Contributor cannot create role assignments. The GitHub deploy identity set up
  in step 4 only needs Contributor.
- **Azure CLI 2.60 or newer** with the Bicep tools: `az bicep install`, `az extension add --name containerapp`.
- **`jq` and `python3`** on the machine you run these steps from: the §5 smoke test and the output reads use them.
- **The Entra sign-in values** (§6): tenant id, the app registration's client id and the two group
  names. They are public identifiers, not secrets. No client secret or certificate is needed, and none
  may be created for this app registration.
- **The Azure AI Foundry endpoint and api-key**, if the Foundry provider is to be used (step 7).

Nothing in this document deletes or changes anything in the Google Cloud deployment. The two run side
by side on separate databases and separate file storage until someone chooses to switch the users over.

```bash
RG=rg-inv-studio                 # the resource group you were given
LOCATION=uaenorth                # one region for everything
SUB=$(az account show --query id -o tsv)
az account set --subscription "$SUB"
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.ContainerRegistry --wait
az provider register --namespace Microsoft.DBforPostgreSQL --wait
az provider register --namespace Microsoft.KeyVault --wait
az provider register --namespace Microsoft.OperationalInsights --wait
az provider register --namespace Microsoft.Storage --wait
az provider register --namespace Microsoft.ManagedIdentity --wait
az provider register --namespace Microsoft.Network --wait
az provider register --namespace Microsoft.Insights --wait
```

## 1. Two values to generate

The database password and the Fernet key that encrypts the `credentials` table. Keep both in a
password manager: the Fernet key cannot be changed later without making every stored provider
credential unreadable. Never paste either into a chat, a task or a commit.

The vault key needs the `cryptography` package, which a plain `python3` usually lacks
(`ModuleNotFoundError: No module named 'cryptography'`). The checkout's `.venv` has it once
`./scripts/setup.sh` has run:

```bash
cd <your checkout of inv-studio>
./scripts/setup.sh                                                                                # once, if .venv is missing
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))'                               # database password
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'  # vault key
```

Write them into a parameter file that stays outside git (`infra/azure/main.parameters.json` is in
`.gitignore`); start from the example and add the two secrets:

```bash
cp infra/azure/main.parameters.example.json infra/azure/main.parameters.json
chmod 600 infra/azure/main.parameters.json
# edit: location, the Entra values below, and add
#   "postgresAdminPassword": { "value": "<the password>" },
#   "vaultKey":              { "value": "<the Fernet key>" }
```

The example also names the Entra sign-in values. The tenant and client ids are required: the template
refuses anything that is not 36 characters long (so the placeholders, and `common` or `organizations`,
fail at deploy time), and in cloud mode the app refuses to start unless both are GUIDs. Until the
platform hands them over, they stay the named placeholders below, and step 2 cannot run.

| Parameter | Value | Where it comes from |
|---|---|---|
| `entraTenantId` | `<AZ_TENANT_ID>` | Entra admin center → Overview → Tenant ID |
| `entraClientId` | `<ENTRA_APP_CLIENT_ID>` | the app registration (§6) → Overview → Application (client) ID |
| `entraAdminGroupName` | `<ENTRA_GROUP_ADMIN_NAME>` | optional; shown on the Users screen only |
| `entraMemberGroupName` | `<ENTRA_GROUP_MEMBER_NAME>` | optional; shown on the Users screen only |

None of these is a credential, and none decides who gets in: that is the app roles and groups in Entra
(§6). The group *names* are display text; the groups' object ids are used only in Entra, when the roles
are granted, and are not a deploy setting.

`networkAddressPrefix` has no default: it is the /24 the platform's IPAM assigns (`<SPOKE_VNET_CIDR>`),
so it cannot overlap the hub. The app subnet takes its first /27, the private endpoints its second.
`hubVnetId` (`<HUB_VNET_ID>`) creates the spoke side of the hub peering; empty creates none.
`dnsServers` (`["<HUB_DNS_IP>"]`) points the network at the hub's DNS; empty keeps Azure DNS.
`internalEnvironment` is `false` for the first, public deploy; the target redeploy sets `true`, which
takes the app's public address away and replaces the environment (it cannot change in place).

The database has no public endpoint, so `psql` from a laptop does not reach it. Run database commands
from inside the app container (`az containerapp exec -n "$APP" -g "$RG" --command bash`; the image has
Python and `psycopg`), or from a machine you put in the network for the purpose.

## 2. Create the resources

**The first deploy is one script.** `scripts/azure_deploy.py` reads the deploy kit (`infra/azure/deploy-kit.example.env`, one line per placeholder of the access request), runs the preflight (sign-in, tenant and subscription, resource group, roles including role assignments, providers, quota, template parameters, then ARM validation and what-if, where Azure Policy refuses), stops at the first check that fails with that reason, and only then deploys. `--check` runs the preflight alone and creates nothing. The steps below are what it runs.

```bash
python3 scripts/azure_deploy.py --kit ~/inv-studio-azure/deploy-kit.env --check
python3 scripts/azure_deploy.py --kit ~/inv-studio-azure/deploy-kit.env
```

```bash
az bicep build --file infra/azure/main.bicep --stdout > /dev/null   # syntax check, no deployment
az deployment group what-if -g "$RG" -f infra/azure/main.bicep -p @infra/azure/main.parameters.json
az deployment group create -n invstudio-infra -g "$RG" \
  -f infra/azure/main.bicep -p @infra/azure/main.parameters.json
az deployment group show -n invstudio-infra -g "$RG" --query properties.outputs -o jsonc
```

About fifteen minutes, most of it the PostgreSQL server and the network-attached Container Apps
environment. The outputs name everything you need below:

```bash
OUT=$(az deployment group show -n invstudio-infra -g "$RG" --query properties.outputs -o json)
APP=$(echo "$OUT"       | python3 -c 'import json,sys; print(json.load(sys.stdin)["appName"]["value"])')
ACR=$(echo "$OUT"       | python3 -c 'import json,sys; print(json.load(sys.stdin)["acrName"]["value"])')
VAULT=$(echo "$OUT"     | python3 -c 'import json,sys; print(json.load(sys.stdin)["vaultName"]["value"])')
APP_URL=$(echo "$OUT"   | python3 -c 'import json,sys; print(json.load(sys.stdin)["appUrl"]["value"])')
echo "$APP_URL"
```

With no image given, the app starts on Microsoft's hello-world image, which is only there to fix the
hostname before the real image exists. `curl "$APP_URL"` answers with a welcome page.

The hostname does not change when the image does, so the redirect URI owner can already add `$APP_URL/`
to the app registration (§6, step 3).

**Key Vault purge protection is on** (`enableKeyVaultPurgeProtection`, default `true`), with 90 days of
soft delete (`keyVaultSoftDeleteRetentionDays`). Nobody, an administrator included, can then destroy
the vault or a secret for good before 90 days have passed. That matters most for `inv-studio-vault-key`:
without it every stored provider credential is unreadable. Both settings are fixed once the vault
exists. Purge protection can never be turned off, and a deleted vault keeps its name, and so this
resource group's vault name, for the 90 days. For a throwaway trial in a resource group that will be
deleted, set `enableKeyVaultPurgeProtection` to `false` and the retention to `7` in the parameter file.

**Deleted and overwritten invoice files expire** (security pack N1). The `evidence` container keeps
blob versioning and 7 days of blob soft delete (`softDeleteDays` in `storage.bicep`). When the app
deletes an invoice, or overwrites a template or learned file, the old content stays behind as a
*previous version*. A lifecycle management rule, `expirePreviousVersions`, deletes previous versions
of block blobs under `evidence/` once they are more than `blobPreviousVersionDays` days old (default
`7`). It never touches the current version of a live file. `evidence` is the account's only container
today; the prefix keeps the rule to it even if another container is added later.

How Azure counts the age, from Microsoft's
[lifecycle policy structure](https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-policy-structure#action-run-conditions)
page: "Rules for previous versions use the creation time of that version when the blob was first
written (not when the version became a previous version)." So the clock starts when the file was
uploaded or last overwritten, not when the app deleted it. Lifecycle deletes are not final either: in
an account with soft delete on, the policy "will put the object in a soft-deleted state that is
retained for the duration of the soft-delete"
([lifecycle overview, known issues](https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-overview#known-issues-and-limitations)).
Microsoft gives no fixed run time: policies "process objects continuously in the background" and a
new or changed policy starts "within 24 hours"
([lifecycle FAQ](https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-policy-faq#is-there-a-way-to-identify-the-time-at-which-the-policy-will-be-executing)).

**From an in-app delete to unrecoverable: at most about 15 days** with the defaults. That is
`blobPreviousVersionDays` (7) for the newest possible version, a file written just before it was
deleted, plus up to 1 day for the background run to reach it, plus `softDeleteDays` (7) in soft delete.
A file written more than 7 days before the delete goes sooner, in about 8 days: it is already old
enough, so the next run deletes it into the 7-day soft delete. The day for the run is what Microsoft
states for a policy to take effect. It is not a guarantee, and a run can take longer when the account
nears its request-rate limit. On the first deployment, add up to 24 hours before the rule is active.
Until then a version can be restored from the portal (the blob's *Versions* tab) or with
`az storage blob undelete` for a soft-deleted one. The same window applies to the previous content of
an overwritten template or learned file.

Check the rule once after the first deployment, and after any change to it:

```bash
STORAGE=$(echo "$OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["blobAccountName"]["value"])')
az storage account management-policy show --account-name "$STORAGE" -g "$RG" \
  --query "policy.rules[?name=='expirePreviousVersions'].{enabled:enabled,prefix:definition.filters.prefixMatch,days:definition.actions.version.delete.daysAfterCreationGreaterThan}"
```

It must print `enabled: true`, prefix `evidence/` and the day count. The portal shows the same rule
under the storage account's *Data management → Lifecycle management*. The policy's deletes are
logged in Log Analytics with every other blob delete (`StorageDelete`).

## 3. Grant yourself access to the vault

The template gives the *app* read access to the secrets. Your own account gets nothing, so add it if
you want to read or set secrets by hand (steps 6 and 7 need this):

```bash
ME=$(az ad signed-in-user show --query id -o tsv)
az role assignment create --assignee "$ME" --role "Key Vault Secrets Officer" \
  --scope "$(az keyvault show -n "$VAULT" -g "$RG" --query id -o tsv)"
```

## 4. Let GitHub Actions deploy, without a stored secret

The deploy workflow signs in with OIDC federated credentials: GitHub presents a short-lived token and
Azure trusts it for this one repository and environment. No key or password is stored in the repository.

```bash
REPO=muttonkodibiriyani/Invoice_studio_Al     # owner/repo of the repository the workflow runs in
APPID=$(az ad app create --display-name inv-studio-deploy --query appId -o tsv)
az ad sp create --id "$APPID"
az role assignment create --assignee "$APPID" --role Contributor \
  --scope "/subscriptions/$SUB/resourceGroups/$RG"
az ad app federated-credential create --id "$APPID" --parameters "{
  \"name\": \"github-environment-azure\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:$REPO:environment:azure\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"
echo "AZURE_CLIENT_ID=$APPID"
echo "AZURE_TENANT_ID=$(az account show --query tenantId -o tsv)"
echo "AZURE_SUBSCRIPTION_ID=$SUB"
```

The `subject` must match the workflow exactly. `deploy-azure.yml` runs in the `azure` environment, so
the subject is `repo:<owner>/<repo>:environment:azure`. If you later remove `environment: azure` from
the workflow, the subject becomes `repo:<owner>/<repo>:ref:refs/heads/main` instead.

Then set these in the GitHub repository (Settings → Secrets and variables → Actions):

| Kind | Name | Value |
|---|---|---|
| Secret | `AZURE_CLIENT_ID` | the `appId` printed above |
| Secret | `AZURE_TENANT_ID` | the tenant id printed above |
| Secret | `AZURE_SUBSCRIPTION_ID` | the subscription id printed above |
| Variable | `AZURE_RESOURCE_GROUP` | `$RG` |
| Variable | `AZURE_CONTAINER_REGISTRY` | `$ACR` |
| Variable | `AZURE_CONTAINER_APP` | `$APP` |
| Variable | `ENTRA_TENANT_ID` | `<AZ_TENANT_ID>`, the same as `entraTenantId` |
| Variable | `ENTRA_CLIENT_ID` | `<ENTRA_APP_CLIENT_ID>`, the same as `entraClientId` |
| Variable | `ENTRA_ADMIN_GROUP_NAME` | the same as `entraAdminGroupName` (empty if that is empty) |
| Variable | `ENTRA_MEMBER_GROUP_NAME` | the same as `entraMemberGroupName` (empty if that is empty) |

None of the three secrets is a credential — they are identifiers — but keeping them as secrets is the
convention and keeps them out of logs. The four `ENTRA_*` variables are public values. The workflow
never sets them on the app: it compares them with what the Bicep deployment set and stops before
building when they differ, or when the app still carries an old Firebase setting.

Add required reviewers to the `azure` environment
(Settings → Environments → azure) so a deployment needs a human approval.

## 5. Build the image and put it on a revision

Run **Actions → Deploy to Azure → Run workflow** from the branch you want to ship, leaving
"Send all traffic to the new revision" **off**. Until the workflow change from task 01a182b3-4fac is
applied, use the hand-build block below and §8 by hand instead: the workflow on main neither sets
`INV_STUDIO_RECOVER_JOBS=0` nor creates the `-live` revision. With that change, the run:

1. pins traffic to the revision that is live now, so the new one starts with no traffic;
2. builds the image inside ACR (`az acr build`) — the image is about 5.6 GB with the OCR models, far
   past what a hosted runner can hold, so the build happens in Azure and nothing large crosses the
   network. The first build takes **40–90 minutes**; later builds reuse the dependency layers;
3. creates a new revision from the image digest, with `INV_STUDIO_RECOVER_JOBS=0`, and waits for it to
   be healthy. That revision is a **candidate**: it shares the live database, so it must not take over
   invoices the live revision is reading (§9, "Invoices in flight");
4. prints the new revision's own address, the app address and the rollback command in the run summary.

The revision starts with no sign-in credential at all (§6). Before testing it, check that it is a candidate.
This must print `0`; anything else means the run did not set it, so do not test that revision:

```bash
az containerapp revision show -n "$APP" -g "$RG" --revision <new revision> \
  --query "properties.template.containers[0].env[?name=='INV_STUDIO_RECOVER_JOBS'].value | [0]" -o tsv
```

Test it on the revision address from the summary
(`https://<app>--<suffix>.<region>.azurecontainerapps.io`) with the smoke test below before §8 sends it
any traffic.

**Smoke test: sign in, and one write must succeed.** Sign-in only works on an address registered as a
redirect URI, and a revision address is not one, so the token comes from the browser on `$APP_URL` and
is then sent to the revision address: the API checks the token, not the page it came from. On the
**first** deployment `$APP_URL` still serves the hello-world image, so the redirect URI owner adds
`$REV_URL/` as a second SPA redirect URI for the test and removes it after §8. Sign in as someone in
`<ENTRA_GROUP_ADMIN>`. The write saves the AI settings back unchanged, so it alters nothing; it passes
only if the token verifies against the tenant's keys and the database takes a write.

```bash
REV_URL=https://<app>--<suffix>.<region>.azurecontainerapps.io   # from the run summary

curl -fsS "$REV_URL/health"                                                  # 200
curl -s -o /dev/null -w '%{http_code}\n' "$REV_URL/api/session"             # 401: no token
curl -fsS "$REV_URL/api/public-config" | jq .entra                            # tenantId and clientId are yours
# A real Entra token for another audience (Microsoft Graph) must be refused:
GRAPH=$(az account get-access-token --resource https://graph.microsoft.com --query accessToken -o tsv)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $GRAPH" "$REV_URL/api/session"  # 401
unset GRAPH
```

Then, in a browser: open the sign-in address, choose **Sign in with Microsoft**, open the
developer tools → Network, click any `/api/` request and copy the value of its `Authorization` header
after `Bearer `. Paste it at the hidden prompt, so it stays out of the shell history:

```bash
read -rsp 'Access token: ' TOKEN; echo
curl -fsS -H "Authorization: Bearer $TOKEN" "$REV_URL/api/session"           # 200: your name, role admin
SETTINGS=$(curl -fsS -H "Authorization: Bearer $TOKEN" "$REV_URL/api/state" | jq -c .settings)
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Origin: $REV_URL" -H 'X-Studio-Request: 1' \
  -H 'Content-Type: application/json' -d "$SETTINGS" "$REV_URL/api/settings"  # 200: THE WRITE
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $TOKEN" -H "Origin: $REV_URL" -H 'X-Studio-Request: 1' \
  -H 'Content-Type: application/json' -d '{}' "$REV_URL/api/users"          # 404: nobody is invited in the app
unset TOKEN SETTINGS
```

Last, sign in once as someone in **neither** group: Entra itself must refuse with `AADSTS50105` and the
app must never be reached. If that person gets in, "Assignment required" is off on the enterprise
application (§6): stop, it is not ready.

If `/api/session` answers 401 with a token from the browser, read the revision's log: a refused token
logs the check that failed (issuer, audience, tenant, scope...) and never the token. The usual cause is
`entraTenantId` or `entraClientId` not matching the registration, or the registration still issuing v1
tokens (§6). A revision that fails this test gets no traffic. Deactivate it, so it stops running next
to the live one: `az containerapp revision deactivate -n "$APP" -g "$RG" --revision <new revision>`.

**First deployment only: the address the app sees.** The `ip` in the request log takes the **last** `X-Forwarded-For` entry (`app/authorization.py`,
`client_ip`). That entry is the caller's own address only if the front end in front of the app adds
it. Cloud Run's does. On Container Apps nobody has checked yet, so check it once on the candidate,
from a computer whose public address you know:

```bash
curl -s https://api.ipify.org; echo                                          # your public address
curl -s -o /dev/null -H 'X-Forwarded-For: 203.0.113.7' "$REV_URL/api/session"   # 401, and one log line
az containerapp logs show -n "$APP" -g "$RG" --revision <new revision> --tail 50 | grep '"route": "/api/session"'
```

The `ip` in that log line must be your public address. If it is `203.0.113.7`, the ingress passes the
header on unchanged and any caller can choose the address the log records. Report it before go-live:
the per-account limits still hold, but the request log's `ip` cannot be trusted.

The first start of a revision is slow: Container Apps has to pull 5.6 GB and the OCR readers warm up.
The startup probe allows about seven minutes. If a revision never becomes healthy, read the logs
(step 9) before assuming the image is wrong.

To build by hand instead of through the workflow:

```bash
az acr build --registry "$ACR" -g "$RG" --platform linux/amd64 --file Dockerfile \
  --image "inv-studio:$(git rev-parse --short HEAD)" --timeout 14400 .
# The live revision is the one with all the traffic, not the newest healthy one (that may be a candidate).
# Only while the traffic is still the template's "latest revision" entry is the newest healthy one live.
TRAFFIC=$(az containerapp ingress traffic show -n "$APP" -g "$RG" -o json)
LIVE=$(echo "$TRAFFIC" | jq -r '[.[] | select(.weight == 100)][0] | .revisionName // ""')
if [ -z "$LIVE" ] && [ "$(echo "$TRAFFIC" | jq -r '[.[] | select(.weight == 100)][0].latestRevision')" = true ]; then
  LIVE=$(az containerapp show -n "$APP" -g "$RG" --query properties.latestReadyRevisionName -o tsv)
fi
echo "Live revision: ${LIVE:-none}"
```

Go on only if that names the revision you expect to be live. `none` means no revision holds all the
traffic: settle it first (§9).

```bash
az containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight "$LIVE=100"   # the new one gets none
az containerapp update -n "$APP" -g "$RG" \
  --image "$(az acr show -n "$ACR" --query loginServer -o tsv)/inv-studio:$(git rev-parse --short HEAD)" \
  --revision-suffix "r$(git rev-parse --short HEAD)" --set-env-vars INV_STUDIO_RECOVER_JOBS=0
```

`--set-env-vars` changes the app's template, not only this revision: every later `az containerapp
update` (a new member list in §6, say) carries `INV_STUDIO_RECOVER_JOBS=0` into its revision until
§8 removes it.

## 5a. Move the app off the database admin login (once, before go-live)

The template's database URL logs in as the server admin, because Azure cannot create a PostgreSQL role
for you and the server is private. Left that way, a SQL injection or a stolen URL owns the whole
server. Do this once, after the first real revision is healthy and before §8, while the database is
still empty (security review M1):

1. **Pick the role's password.** Generate it the same way as in §1
   (`.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))'`) and keep it in the
   password manager next to the admin password.
2. **Create the role from inside the app container.** At this point the container's
   `INV_STUDIO_DATABASE_URL` is the admin URL, so you never type the admin password:

   ```bash
   az containerapp exec -n "$APP" -g "$RG" --command bash
   # inside the container:
   python scripts/azure_db_app_role.py create     # asks for the new password twice, input hidden
   exit
   ```

   It creates the login role `invstudio_app`: no superuser, CREATEDB, CREATEROLE or replication, and
   not a member of `azure_pg_admin`. It gets CONNECT and TEMPORARY on database `invstudio` and USAGE
   and CREATE on schema `public`, and it takes over every table the app has created there. The app
   creates and alters its own tables at start, so it has to own them. It needs nothing server-wide.
   The password reaches the server only as a SCRAM verifier. The script then logs in as the new role
   with `sslmode=verify-full` and stops with `FAILED:` if anything is wrong. The running revision keeps
   working meanwhile: the admin keeps access to the tables it handed over. Running it again only re-sets
   the password.
3. **Point the app at the new role.** Run this on your own machine. The password is read with
   `read -rs`, so it stays out of the shell history:

   ```bash
   DB_HOST=$(echo "$OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["databaseHost"]["value"])')
   read -rsp 'invstudio_app password: ' APP_DB_PASSWORD; echo
   DB_URL="postgresql://invstudio_app:$(printf %s "$APP_DB_PASSWORD" | python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')@$DB_HOST:5432/invstudio?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt"
   SECRET_FILE=$(mktemp)   # mode 0600; --file keeps the URL out of the process list
   printf %s "$DB_URL" > "$SECRET_FILE"
   az keyvault secret set --vault-name "$VAULT" --name inv-studio-database-url --file "$SECRET_FILE" --encoding utf-8 --output none
   rm -f "$SECRET_FILE"; unset APP_DB_PASSWORD DB_URL SECRET_FILE
   REV=$(az containerapp revision list -n "$APP" -g "$RG" --query "[?properties.active] | [-1].name" -o tsv)
   az containerapp revision restart -n "$APP" -g "$RG" --revision "$REV"
   ```

4. **Check it.** This is a go-live check, run again on every new deployment:

   ```bash
   az containerapp exec -n "$APP" -g "$RG" --command bash
   # inside the container:
   python scripts/azure_db_app_role.py check
   ```

   It must print `OK: certificate verified, least-privilege role, owns every app table`. It connects
   with the URL the app uses and fails unless the server certificate was verified (`sslmode=verify-full`,
   TLS in use) and the login is `invstudio_app`, a plain role that owns every app table. Then repeat the
   §5 smoke test: the write must still succeed.

From now on, **every re-run of step 2 needs `"writeSecrets": { "value": false }`**. With `true` the
template writes the admin URL back into Key Vault, and the app runs as the admin again from its next
restart. `check` catches that.

**The certificate check.** The URL uses `sslmode=verify-full`: the server's certificate must chain to
a root in the image's Debian bundle (`/etc/ssl/certs/ca-certificates.crt`) and name the host. Azure's
certificates chain to DigiCert Global Root G2 or Microsoft RSA Root CA 2017. Both are in that bundle
(Microsoft's [TLS page](https://learn.microsoft.com/en-us/azure/postgresql/security/security-tls)
names them). The host is the server's usual `<server>.postgres.database.azure.com` name, which the
private zone points at the private endpoint, not the `privatelink` name. The bundle is named outright,
because `sslrootcert=system` does not find it: psycopg-binary's own OpenSSL looks in its build
directory. If a revision never becomes healthy and its log shows `certificate verify failed` or
`does not match host name`, stop and report it. Do not lower the URL to `sslmode=require`: that
accepts any certificate. Microsoft's fallback for DNS set-ups where the name cannot match is
`verify-ca`, and choosing it is a security decision for the owner.

## 6. Microsoft Entra ID sign-in

Sign-in is the team's workforce Entra ID tenant and nothing else. The design is the tm8 doc "Entra ID
sign-in design"; this section is what to set up and check. Every `<PLACEHOLDER>` is a value the
platform has not handed over yet: leave it as it is until it is given, and never make one up.

**The app holds no Entra credential.** The browser signs in as a single-page application (auth code with
PKCE). The API only checks access tokens against the tenant's public signing keys, at
`https://login.microsoftonline.com/<AZ_TENANT_ID>/discovery/v2.0/keys`. There is no client secret, no
certificate and no Microsoft Graph call. Do not create a secret for this registration: it would be a
long-lived credential that nothing uses.

**What the Entra administrator sets up (once).** Agents do not create these; Tarkesh or the platform's
Entra administrator does, in the Entra admin center:

1. **App registration** `<ENTRA_APP_DISPLAY_NAME>`: single tenant (accounts in `<AZ_TENANT_ID>` only).
   Platform **Single-page application**; implicit grant off (both boxes); "Allow public client flows"
   **No**; no client secrets or certificates. Its Application (client) ID is `<ENTRA_APP_CLIENT_ID>`.
2. **Expose an API:** Application ID URI `api://<ENTRA_APP_CLIENT_ID>`, one scope `access_as_user`,
   admin consent only. In the manifest set `api.requestedAccessTokenVersion` to **2** (the API refuses v1
   tokens). API permissions: `openid`, `profile`, `offline_access` and the app's own `access_as_user`,
   with admin consent. No Microsoft Graph permission, not even `User.Read`. No groups claim.
3. **Redirect URI**, after step 2 has printed `$APP_URL`: add `$APP_URL/` (with the trailing slash) as a
   **Single-page application** redirect URI. Exact HTTPS addresses only, no wildcards, no `localhost`.
   Done by `<ENTRA_REDIRECT_URI_OWNER>`, who must be an owner of the registration. Add
   `https://<CUSTOM_DOMAIN>/` too if a custom domain is ever used.
4. **App roles** on the registration, both for Users/Groups: display name "Invoice Studio administrator",
   value **`admin`**; display name "Invoice Studio member", value **`member`**. The values are fixed by
   the code; renaming one is a code change.
5. **Enterprise application** (the registration's service principal) → Properties: **Assignment
   required = Yes**. Then Users and groups: grant `admin` to `<ENTRA_GROUP_ADMIN>` and `member` to
   `<ENTRA_GROUP_MEMBER>`. Granting a role to a *group* needs Entra ID P1 or higher in the tenant;
   without it, people are granted one by one (an open platform question).
6. **Group owners** `<ENTRA_GROUP_OWNERS>` add and remove people. Conditional Access (MFA, device,
   location) is the tenant's choice: `<ENTRA_CA_NOTES>`.

**What goes where.**

| Value | Bicep parameter (§1) | GitHub variable (§4) | App setting (§12) |
|---|---|---|---|
| `<AZ_TENANT_ID>` | `entraTenantId` | `ENTRA_TENANT_ID` | `INV_STUDIO_ENTRA_TENANT_ID` |
| `<ENTRA_APP_CLIENT_ID>` | `entraClientId` | `ENTRA_CLIENT_ID` | `INV_STUDIO_ENTRA_CLIENT_ID` |
| `<ENTRA_GROUP_ADMIN_NAME>` | `entraAdminGroupName` | `ENTRA_ADMIN_GROUP_NAME` | `INV_STUDIO_ENTRA_ADMIN_GROUP_NAME` |
| `<ENTRA_GROUP_MEMBER_NAME>` | `entraMemberGroupName` | `ENTRA_MEMBER_GROUP_NAME` | `INV_STUDIO_ENTRA_MEMBER_GROUP_NAME` |

Change a value in the parameter file and re-run step 2 (with `containerImage` set to the live image,
§7), then update the GitHub variable to match. Never set these on the app with `--set-env-vars`: the
deploy workflow would then stop on a mismatch, and the next template run would undo it.

**How access works once it is live.**

- **Entra decides who gets in and with which role.** To give someone access, a group owner adds them to
  `<ENTRA_GROUP_ADMIN>` or `<ENTRA_GROUP_MEMBER>`; to take it away, they remove them. Someone in both
  is an administrator. The app reads the role from the token on every request and never grants,
  changes or removes access. There is no invite, no password and no "Forgot password?" on Azure.
- **The Users screen is read-only.** It lists who has signed in and the role they had at their last
  sign-in, with the two group names from the parameters. It cannot add, disable or delete anyone.
- **A removal is not instant.** A token already issued stays valid until it expires, by default 60–90
  minutes. For an immediate stop: in Entra, "Revoke sessions" on the user and, if needed, set the
  enterprise application's "Enabled for users to sign in" to No; then stop the revision. A shorter token
  lifetime is a platform choice (open question).
- **The only outbound sign-in traffic** is to `login.microsoftonline.com`: the keys and metadata from
  the server, the token calls from the browser. A firewall allow-list needs that name and no Google one.
- **Entra's sign-in logs** (Entra admin center → Sign-in logs, filtered to `<ENTRA_APP_CLIENT_ID>`) are the
  record of every sign-in. Whether they go to the Log Analytics workspace is an open platform question.

**Open platform questions** (placeholders until answered): Entra ID P1 for group-to-role assignment; a
shorter access-token lifetime; sign-in logs to Log Analytics; public cloud or a sovereign authority (the
app assumes `https://login.microsoftonline.com`).

## 7. Azure AI Foundry

The Foundry resource lives in someone else's subscription; the app only needs the endpoint, the
deployment name and the api-key.

```bash
az keyvault secret set --vault-name "$VAULT" --name inv-studio-foundry-key --value '<api-key>' --output none
az containerapp update -n "$APP" -g "$RG" \
  --set-env-vars "INV_STUDIO_AZURE_ENDPOINT=https://<resource>.openai.azure.com" \
                 "INV_STUDIO_AZURE_DEPLOYMENT=<deployment name>"
```

The key reaches the app as `INV_STUDIO_AZURE_API_KEY`. An administrator can also connect Foundry from
the app's Engines & AI page; that copy is encrypted in the `credentials` table.

Or put `foundryEndpoint` and `foundryDeployment` in the parameter file and re-run step 2 — with
`"writeSecrets": { "value": false }` added, so the secrets you set by hand are not overwritten. After §5a
that is not optional: `true` puts the admin database URL back.

**Before re-running step 2 on a live deployment**, set `containerImage` to the image that is live now:

```bash
az containerapp show -n "$APP" -g "$RG" --query "properties.template.containers[0].image" -o tsv
```

The template sends all traffic to the latest revision. With `containerImage` empty it creates a
revision on the hello-world image, and that takes the app down until you move traffic back (§9).

## 8. Go live

Only after the revision has been tested on its own address, and only once §5a's `check` has printed
`OK`: the app verifies the database certificate and does not run as the admin.

The tested candidate does not get the traffic: it runs with `INV_STUDIO_RECOVER_JOBS=0`, so as the live
revision it would never queue again the invoices of an instance that stopped. Go live on a fresh
revision from the **same image digest** without that setting, check it on its own address, then move
the traffic and stop the candidate:

```bash
CANDIDATE=<new revision>                                   # from the run summary
IMAGE=$(az containerapp revision show -n "$APP" -g "$RG" --revision "$CANDIDATE" \
  --query "properties.template.containers[0].image" -o tsv)  # <registry>/inv-studio@sha256:...
az containerapp update -n "$APP" -g "$RG" --image "$IMAGE" \
  --revision-suffix "${CANDIDATE#"$APP"--}-live" --remove-env-vars INV_STUDIO_RECOVER_JOBS
az containerapp revision show -n "$APP" -g "$RG" --revision "$CANDIDATE-live" \
  --query "{health:properties.healthState,fqdn:properties.fqdn}" -o tsv   # wait for Healthy
curl -s "https://<that fqdn>/health"
az containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight "$CANDIDATE-live=100"
curl -s "$APP_URL/health"
az containerapp revision deactivate -n "$APP" -g "$RG" --revision "$CANDIDATE"
```

Until the workflow change from task 01a182b3-4fac is applied, go live by hand as above; do not use the
workflow's traffic switch. With that change, running the workflow with **"Send all traffic to the new
revision" on** does the same in one run:
candidate, then the `-live` revision from the same digest, then the traffic. It builds and tests
nothing by hand in between, so use it only for an image already tested as a candidate (`image_tag`).

## 9. Roll back

The app runs in multiple-revision mode, so every earlier revision is still there and a rollback is a
traffic switch — seconds, no rebuild, the same shape as `gcloud run services update-traffic`.

```bash
az containerapp revision list -n "$APP" -g "$RG" \
  -o table --query "[].{revision:name,created:properties.createdTime,active:properties.active,health:properties.healthState,traffic:properties.trafficWeight}"
az containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight <previous revision>=100
```

A rolled-back revision runs the older code against the same database, so schema changes must stay
additive, exactly as on Cloud Run (`docs/RUNBOOK.md` §2).

If the problem is the database rather than the code, PostgreSQL Flexible Server restores to a point in
time into a **new** server (`az postgres flexible-server restore`); it never overwrites the original.

### Invoices in flight while two revisions overlap

During a traffic switch, a rollback or a replica restart, two app processes can share the one database
for a while. Each unfinished invoice records which process holds it, and that process renews a heartbeat
on it every 30 s (security review M5):

- **A process takes over another's invoice only once the heartbeat is 120 s old.** At that point the holder
  has stopped or stalled. An invoice that a live revision is still reading stays with that revision, and it
  finishes there even after traffic has moved.
- **When an old process stops, its unfinished invoices are queued again within about two and a half
  minutes**, oldest first, by whichever running revision notices first. A user sees "processing" for that
  long, and no invoice is lost.
- **A stalled holder that wakes up cannot overwrite the invoice.** A result, a failure or a progress update
  is saved only while the process still holds the invoice. Otherwise it is dropped, and the audit log
  records `stale_result_dropped`. If the stalled process had already sent the invoice to the AI, that read
  is paid twice.
- **A revision with `INV_STUDIO_RECOVER_JOBS=0` never takes over**: the no-traffic candidate of §5,
  as in `docs/RUNBOOK.md` §2. It still finishes its own invoices. A live revision must not have the
  setting, which is why §8 goes live on a fresh revision. To check a revision, read its env as in §5.
- An invoice recorded by a version from before holders existed has no heartbeat, so the first newer
  revision takes it over at start, as before.

## 10. Logs and health

```bash
# the last log lines, live
az containerapp logs show -n "$APP" -g "$RG" --follow --tail 100
# anything the app logged as an error in the last hour
az monitor log-analytics query -w "$(az monitor log-analytics workspace show -g "$RG" -n invstudio-logs --query customerId -o tsv)" \
  --analytics-query "ContainerAppConsoleLogs_CL | where TimeGenerated > ago(1h) | where Log_s contains '\"severity\": \"ERROR\"' | project TimeGenerated, Log_s | order by TimeGenerated desc" \
  -o table
```

The app writes JSON lines when `INV_STUDIO_CLOUD=1`, which is how it ran on Cloud Run, so the fields
are the same. Not yet set up, and worth adding once the app is in use: an availability test on
`/health` and alert rules for the 5xx ratio, container restarts and database storage — the equivalents
of the four "Invoice Studio: …" policies in the GCP project.

## 11. What it costs

The monthly cost, line by line, with its price source and the open questions, is in
[What it costs per month](#what-it-costs-per-month) at the top. Confirm in the Azure pricing calculator
before committing to anything.

## 12. Settings the app reads

The template sets all of these; this is the list to check when something is misconfigured.

| Variable | Value |
|---|---|
| `INV_STUDIO_CLOUD` | `1` — PostgreSQL store, Entra ID sign-in, JSON logs |
| `INV_STUDIO_STORAGE_BACKEND` | `azure` — files go to Blob Storage. Unset or `gcs` is the Google path, which is what the Cloud Run deployment keeps using |
| `INV_STUDIO_AZURE_STORAGE_ACCOUNT` | the storage account name; the app builds `https://<account>.blob.core.windows.net` from it |
| `INV_STUDIO_BUCKET` | `evidence` — the blob container, the same setting the GCS bucket used |
| `INV_STUDIO_DATABASE_URL` | from Key Vault `inv-studio-database-url`: `postgresql://invstudio_app:<password>@<server>.postgres.database.azure.com:5432/invstudio?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt` after §5a (the template writes the admin login until then) |
| `INV_STUDIO_VAULT_KEY` | from Key Vault `inv-studio-vault-key` |
| `INV_STUDIO_ALLOWED_ORIGINS` | the app URL, plus anything in the `extraAllowedOrigins` parameter. The API refuses writes from origins it does not know |
| `INV_STUDIO_DATA` | `/tmp/inv-studio-data` — scratch space only; everything that must survive a restart is in PostgreSQL or Blob Storage |
| `AZURE_CLIENT_ID` | the managed identity's client id, so `DefaultAzureCredential` picks it |
| `INV_STUDIO_ENTRA_TENANT_ID` | `entraTenantId`. The app refuses to start in cloud mode unless it is a GUID (not `common`, `organizations` or `consumers`) |
| `INV_STUDIO_ENTRA_CLIENT_ID` | `entraClientId`: the audience the API accepts and the client the browser signs in with. Must be a GUID |
| `INV_STUDIO_ENTRA_ADMIN_GROUP_NAME`, `INV_STUDIO_ENTRA_MEMBER_GROUP_NAME` | `entraAdminGroupName`, `entraMemberGroupName`: display text on the Users screen only |
| `INV_STUDIO_ALLOWED_HOSTS` | `*.<environment domain>` — the app answers an unknown `Host` with 400; this lets a revision's own address (§5) through. The startup probe sends the app's hostname as `Host` for the same reason |
| `INV_STUDIO_AZURE_API_KEY` | from Key Vault `inv-studio-foundry-key` |
| `INV_STUDIO_AZURE_ENDPOINT`, `INV_STUDIO_AZURE_DEPLOYMENT` | `foundryEndpoint`, `foundryDeployment`, only when set |

The app refuses to start in cloud mode if an old Firebase setting is still present: `GOOGLE_CLOUD_PROJECT`,
any `INV_STUDIO_FIREBASE_*`, `INV_STUDIO_ALLOWED_EMAILS` or `INV_STUDIO_MEMBER_EMAILS`.

One more is set only on a candidate revision, never by the template: `INV_STUDIO_RECOVER_JOBS=0`
— the revision takes over no unfinished invoice from another (§5, §9). Unset means on.

Two more exist for development and are not set in Azure:

- `INV_STUDIO_AZURE_STORAGE_CONNECTION_STRING` — a connection string, which is how a laptop or a test
  run talks to Azurite. It does not work against the account this template creates: shared-key access
  is turned off there, and the app uses the managed identity.
- `INV_STUDIO_AZURE_STORAGE_URL` / `INV_STUDIO_AZURE_STORAGE_SUFFIX` — for a sovereign cloud or a
  storage emulator, when the account name alone is not enough to build the endpoint.

The Azure client libraries live in the optional `azure` extra: `pip install -e '.[cloud,azure]'`. The
image installs it, so one image runs on both clouds.

## 13. Known limits of phase 1

- **Closed: the database had a public endpoint.** The Container Apps environment now runs in a delegated
  subnet of `<prefix>-vnet`, the server has a private endpoint in a second subnet, the zone
  `privatelink.postgres.database.azure.com` resolves its usual name to the private address, and
  `publicNetworkAccess` is `Disabled`, with no firewall rules. An environment cannot be moved into a
  network after it is created, so a deployment made from an older version of this template has to
  recreate the environment.
- **A Consumption workload profile caps a replica at 4 vCPU / 8 GiB.** The environment is now a
  workload-profiles environment, so if the OCR readers need more memory, add a Dedicated D4 profile in
  place (`az containerapp env workload-profile add --workload-profile-type D4`) and move the app onto it
  (`az containerapp update --workload-profile-name ...`). That gives 4 vCPU / 16 GiB, is billed per
  node-hour and adds the plan management fee. Kept open until a real workload shows 8 GiB is not enough.
- **Closed: the app did not start on Azure.** It looked for a Google credential at start-up, and the
  template ships none, and its startup probe arrived with the replica's IP as `Host`, which the app
  answers with 400. Sign-in is now Entra ID with no credential at all, the probe sends the app's
  hostname and `INV_STUDIO_ALLOWED_HOSTS` admits revision addresses; the §5 smoke test proves a
  signed-in write on each new revision.
- **Closed: Firebase was still in the picture.** Sign-in depended on a Google project shared with
  another product, and access on Azure was given through e-mail lists. Sign-in is now the team's own
  Entra ID tenant, with roles from two groups (§6). What stays open: a removal takes effect when the
  user's current token expires (§6), and group-to-role assignment needs Entra ID P1.
- **No user moves across.** Google Cloud sign-ins are not carried over to Azure; people get access by
  being put in the Entra groups.
- **Nothing migrates data.** The Azure deployment starts with an empty database and an empty blob
  container. Moving the existing invoices across (`pg_dump` / `pg_restore` plus `azcopy` for the
  bucket) has not been written down yet and needs its own plan and its own downtime window. With the
  database private, the restore has to run from inside the network.
- **No infrastructure drift check.** Re-running step 2 is idempotent as long as `containerImage`
  names the live image (§7), but nothing warns when someone changes a setting through the portal.
- **Security status lives in [SECURITY.md](SECURITY.md)**: the go-live conditions (including §5a), and the
  findings still open here, such as the public endpoints of the vault, the storage account and the
  registry (L4). If those are closed, the deploy workflow's `az acr build` must still reach a private
  registry, through trusted-services access or a dedicated agent pool, which costs extra.
