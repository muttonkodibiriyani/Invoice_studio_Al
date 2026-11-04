# Deploy Invoice Studio to Azure from Cloud Shell

Every step is one command in **Azure Cloud Shell (Bash)**, in this order. Each one checks before it changes
anything and stops with one plain reason if something is missing. [AZURE.md](AZURE.md) has the detail behind each
step; [INFOSEC_RESPONSE.md](INFOSEC_RESPONSE.md) maps each InfoSec point to the change that closes it.

You need: Owner (or Contributor + Role Based Access Control Administrator) on an empty resource group, the filled
deploy kit (the platform team's answer to the access request), and an account in the Entra admin group.

## 0. Open Cloud Shell and get the code

```bash
git clone https://github.com/muttonkodibiriyani/Invoice_studio_Al.git
cd Invoice_studio_Al
git checkout main            # or the release branch you were told to ship
az account set --subscription <AZ_SUBSCRIPTION_ID>
```

## 1. Fill in the deploy kit (once)

```bash
mkdir -p ~/inv-studio-azure && chmod 700 ~/inv-studio-azure
cp infra/azure/deploy-kit.example.env ~/inv-studio-azure/deploy-kit.env
chmod 600 ~/inv-studio-azure/deploy-kit.env
nano ~/inv-studio-azure/deploy-kit.env
```

Write `n/a` for a line that does not apply. Names and ids only: no password, key or token goes in the kit. The two
optional InfoSec lines at the end are worth filling in: `ALERT_EMAILS` (who gets the alerts) and
`INGRESS_ALLOWED_CIDRS` (which client ranges may reach the app).

## 2. Preflight: nothing is created

```bash
./scripts/cloudshell.sh check
```

It checks the kit, your sign-in, tenant and subscription, the resource group, your roles, resource providers,
quota, the template, and Azure Policy (through what-if). Fix what it names, then run it again until it passes.

## 3. Create the resources (about 15 minutes)

```bash
./scripts/cloudshell.sh infra
```

It asks for the database admin password and the vault key (hidden; generate them as AZURE.md §1 shows, keep both in
the password manager), shows the what-if, and waits for you to type `DEPLOY`. What it creates, with InfoSec's points
built in:

- spoke network, both subnets behind NSGs; **outbound limited to named Azure services, internet denied**; optional
  route through the hub firewall;
- PostgreSQL 16, private only; Key Vault and Blob with **private endpoints** (public still on until step 8);
- the container app with the **AI provider limit** (`azure` only) and optional client IP ranges;
- **alerts** on restarts, 5xx, database CPU and storage.

At the end it prints the app address. **Ask the Entra admin to add `<app address>/` as a single-page application
redirect URI** on the app registration (AZURE.md §6).

## 4. Give yourself access to the vault

```bash
./scripts/cloudshell.sh vault-access
```

Steps 6 and 7 read and write the vault from Cloud Shell. Wait a few minutes before step 6.

## 5. Build the image (40 to 90 minutes the first time)

```bash
./scripts/cloudshell.sh build
```

ACR builds it inside Azure. Cloud Shell closes after 20 idle minutes: if it does, the build carries on in ACR. Check
it with `az acr task list-runs -r "$ACR" -o table` (after `source ~/inv-studio-azure/outputs.env`) and run
`./scripts/cloudshell.sh build` again only if it failed.

## 6. Put the first image on the app

```bash
./scripts/cloudshell.sh image
```

Runs the template again with the image, keeping every secret, and waits until the new revision is Healthy and
`/health` answers. Then sign in at the app address once as an admin.

## 7. Move the app off the database admin login

```bash
./scripts/cloudshell.sh db-role
```

Opens a shell in the container. Run `python scripts/azure_db_app_role.py create`, choose the new password, `exit`,
then do AZURE.md §5a step 3 (store the new database URL in the vault and restart).

## 8. Lock down Key Vault and Blob

```bash
./scripts/cloudshell.sh lockdown
```

Checks both private endpoints are Approved with their DNS record, switches off the public endpoints, restarts the
app and waits until it is Healthy. Paste an admin token when asked (docs/API.md §2) and it also proves the app
reaches the database and Blob through the private endpoints. **If anything fails it opens them again by itself**,
so the app keeps working, and tells you why. `./scripts/cloudshell.sh lockdown --undo` opens them for maintenance;
run `lockdown` again afterwards.

## 9. Test the deployment

```bash
./scripts/cloudshell.sh test
```

Paste an admin token. Runs the Postman collection with newman: the smoke test, the security checks (no token,
forged token, missing request header, foreign origin, schema pages, member on admin routes, non-Azure AI provider)
and the invoice flow (upload the synthetic sample, read it locally, delete it). Every line must show a tick. With
`MEMBER_TOKEN=<a member's token>` set first, the member checks run too. For the full attack pack see
[ATTACK_PACK.md](ATTACK_PACK.md).

## 10. Check, any time

```bash
./scripts/cloudshell.sh status
```

Revision health and traffic, whether the vault and Blob are locked down, the alerts, and `/health`.

## Later images

Every image after the first goes on through **Actions → Deploy to Azure** as a candidate, then AZURE.md §8. Do not
run `infra` or `image` again on the running app (AZURE.md §5 explains why). Switching AI on is AZURE.md §7, with the
managed identity.

## If a step stops

| Message starts with | Do |
|---|---|
| `STOP: deploy kit` | Fix that line of the kit. |
| `STOP: signed in ...` | `az account set --subscription ...`, or sign in as the kit's user. |
| `STOP: ... roles` | Ask for the role it names on the resource group. |
| `STOP: ... policy` | The what-if names the policy; ask for the exemption (kit line `AZ_POLICY_EXEMPTIONS`). |
| `FAILED: the deployment stopped` | Run the `az deployment operation group list` command it prints, fix, then `python3 scripts/azure_deploy.py --kit ~/inv-studio-azure/deploy-kit.env --rerun`. |
| `FAILED` on the Container Apps environment, mentioning images, network or outbound | Rare: the region's platform needs outbound traffic that Microsoft's list does not name. Add `RESTRICT_EGRESS=false` to the kit, `--rerun`, and tell InfoSec; the hub firewall (`HUB_FIREWALL_IP`) then does the filtering. |
| `cannot read inv-studio-database-url` | Step 4, wait a few minutes, run again. |
| `FAILED: ... after the lockdown` | The app is already open again. Usually DNS: with `HUB_DNS_IP` set, the hub must answer the `privatelink.vaultcore.azure.net` and `privatelink.blob.core.windows.net` zones, or pass them in `PDNS_ZONE_IDS`. |
