# Deploy Invoice Studio from scratch: copy-paste commands for Azure Cloud Shell

Open **portal.azure.com → Cloud Shell (the `>_` icon) → Bash**. Paste one block at a time, in order. Wait for
each block to finish before you paste the next.

You edit only **Step 1** (your names). After that, paste every block as it is.

The scripts check before they change anything. If one stops, it prints one line starting `STOP:` or `FAILED:`
that says what to fix. The table at the end of [DEPLOY_CLOUD_SHELL.md](DEPLOY_CLOUD_SHELL.md) lists them.
[AZURE.md](AZURE.md) has the detail behind each step.

**What you need:**

- Owner on the subscription, or on the resource group you will use.
- Permission to create an app registration in Entra ID. Step 4 needs an Entra admin for one command.
- About 2½ hours. Most of that is the image build in Step 7, which runs on its own inside Azure.

**If Cloud Shell disconnects** (it closes after 20 idle minutes), open it again and paste this. Then carry on
from the step you were on:

```bash
cd ~/Invoice_studio_Al && source ~/inv-studio-azure/me.env && [ -f ~/inv-studio-azure/outputs.env ] && source ~/inv-studio-azure/outputs.env; echo "ready: $RG"
```

---

## Step 1. Your values (edit these 6 lines, then paste)

```bash
mkdir -p ~/inv-studio-azure && chmod 700 ~/inv-studio-azure
cat > ~/inv-studio-azure/me.env <<'EOF'
export RG=rg-inv-studio                 # resource group to create (or the empty one you were given)
export REGION=uaenorth                  # one region for everything: uaenorth, westeurope, ...
export PREFIX=invstudio                 # 3-9 lower-case letters/digits; resource names start with it
export VNET_CIDR=10.60.0.0/24           # a free /24 address range for the app's network
export ALERT_EMAIL=you@example.com      # who gets the alert emails
export APP_REG_NAME="Invoice Studio"    # the name of the sign-in app in Entra ID
EOF
chmod 600 ~/inv-studio-azure/me.env
source ~/inv-studio-azure/me.env && echo "saved: $RG in $REGION"
```

## Step 2. Get the code and choose the subscription

```bash
cd ~ && git clone https://github.com/muttonkodibiriyani/Invoice_studio_Al.git
cd ~/Invoice_studio_Al && git checkout main && git log --oneline -1
az account list -o table
```

Copy the **SubscriptionId** you want from that table, put it between the quotes on the first line below, then paste the block:

```bash
az account set --subscription "PASTE-YOUR-SUBSCRIPTION-ID-HERE"
cat >> ~/inv-studio-azure/me.env <<EOF
export SUB_ID=$(az account show --query id -o tsv)
export TENANT_ID=$(az account show --query tenantId -o tsv)
export MY_UPN=$(az ad signed-in-user show --query userPrincipalName -o tsv)
export MY_ID=$(az ad signed-in-user show --query id -o tsv)
EOF
source ~/inv-studio-azure/me.env
echo "subscription $SUB_ID, tenant $TENANT_ID, you are $MY_UPN"
```

## Step 3. Tools, resource providers and the resource group

Registering the providers takes a few minutes the first time.

```bash
source ~/inv-studio-azure/me.env
az bicep install 2>/dev/null; az bicep upgrade 2>/dev/null
az extension add --name containerapp --upgrade --only-show-errors
for p in Microsoft.App Microsoft.ContainerRegistry Microsoft.DBforPostgreSQL Microsoft.KeyVault \
         Microsoft.OperationalInsights Microsoft.Storage Microsoft.ManagedIdentity Microsoft.Network \
         Microsoft.Insights Microsoft.CloudShell; do
  echo "registering $p"; az provider register --namespace "$p" --wait
done
az group create -n "$RG" -l "$REGION" -o table
```

## Step 4. Create the Microsoft sign-in app (Entra ID)

This makes the app registration from AZURE.md §6: a single-page app with no secret, an `access_as_user`
scope, v2 tokens, and the two roles `admin` and `member`. It also makes you an `admin`.

```bash
source ~/inv-studio-azure/me.env
WORK=$(mktemp -d)

# 1. The two app roles. The values admin and member are fixed by the code.
cat > "$WORK/roles.json" <<EOF
[{"allowedMemberTypes":["User"],"displayName":"Invoice Studio administrator","description":"Administers Invoice Studio",
  "isEnabled":true,"value":"admin","id":"$(cat /proc/sys/kernel/random/uuid)"},
 {"allowedMemberTypes":["User"],"displayName":"Invoice Studio member","description":"Uses Invoice Studio",
  "isEnabled":true,"value":"member","id":"$(cat /proc/sys/kernel/random/uuid)"}]
EOF

# 2. The app registration: this tenant only, no implicit grant, no public client.
CLIENT_ID=$(az ad app create --display-name "$APP_REG_NAME" --sign-in-audience AzureADMyOrg \
  --app-roles @"$WORK/roles.json" --enable-access-token-issuance false --enable-id-token-issuance false \
  --is-fallback-public-client false --query appId -o tsv)
echo "export CLIENT_ID=$CLIENT_ID" >> ~/inv-studio-azure/me.env
echo "client id: $CLIENT_ID"

# 3. Expose the API: api://<client id>, one admin-consented scope access_as_user, v2 access tokens.
SCOPE_ID=$(cat /proc/sys/kernel/random/uuid)
az ad app update --id "$CLIENT_ID" --identifier-uris "api://$CLIENT_ID"
cat > "$WORK/api.json" <<EOF
{"api":{"requestedAccessTokenVersion":2,"oauth2PermissionScopes":[{"id":"$SCOPE_ID","type":"Admin","isEnabled":true,
  "value":"access_as_user","adminConsentDisplayName":"Use Invoice Studio as the signed-in user",
  "adminConsentDescription":"Lets the Invoice Studio web page call its API as the signed-in user."}]}}
EOF
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications(appId='$CLIENT_ID')" \
  --headers Content-Type=application/json --body @"$WORK/api.json"

# 4. The permissions the page asks for: openid, profile, offline_access and its own access_as_user.
#    No other Microsoft Graph permission, not even User.Read.
cat > "$WORK/perms.json" <<EOF
{"requiredResourceAccess":[
 {"resourceAppId":"00000003-0000-0000-c000-000000000000","resourceAccess":[
   {"id":"37f7f235-527c-4136-accd-4a02d197296e","type":"Scope"},
   {"id":"14dad69e-099b-42c9-810b-d002981feec1","type":"Scope"},
   {"id":"7427e0e9-2fba-42fe-b0c0-848c9e6a8182","type":"Scope"}]},
 {"resourceAppId":"$CLIENT_ID","resourceAccess":[{"id":"$SCOPE_ID","type":"Scope"}]}]}
EOF
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications(appId='$CLIENT_ID')" \
  --headers Content-Type=application/json --body @"$WORK/perms.json"

# 5. The enterprise application: only people given a role may sign in.
az ad sp create --id "$CLIENT_ID" -o none
az ad sp update --id "$CLIENT_ID" --set appRoleAssignmentRequired=true

# 6. Make yourself an admin of the app.
SP_ID=$(az ad sp show --id "$CLIENT_ID" --query id -o tsv)
ADMIN_ROLE=$(az ad app show --id "$CLIENT_ID" --query "appRoles[?value=='admin'].id" -o tsv)
az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
  --headers Content-Type=application/json \
  --body "{\"principalId\":\"$MY_ID\",\"resourceId\":\"$SP_ID\",\"appRoleId\":\"$ADMIN_ROLE\"}" -o none
rm -rf "$WORK"
echo "Sign-in app ready. Client id $CLIENT_ID; you are an admin."
```

Then grant consent. This needs an Entra admin (Global Administrator, Privileged Role Administrator or Cloud
Application Administrator). If that is not you, send them this one line:

```bash
az ad app permission admin-consent --id "$CLIENT_ID"
```

> If you are not allowed to create app registrations, the commands above stop with `Insufficient privileges`.
> In that case ask the Entra admin to do AZURE.md §6 steps 1, 2, 4 and 5 and send you the **Application
> (client) ID**. Then paste `echo "export CLIENT_ID=<the id>" >> ~/inv-studio-azure/me.env`.

## Step 5. Write the deploy kit

This fills `~/inv-studio-azure/deploy-kit.env` from your values. It holds no password or key.

```bash
source ~/inv-studio-azure/me.env
cat > ~/inv-studio-azure/deploy-kit.env <<EOF
AZ_SUBSCRIPTION_ID=$SUB_ID
AZ_TENANT_ID=$TENANT_ID
AZ_RESOURCE_GROUP=$RG
AZ_REGION=$REGION
AZ_REQUIRED_TAGS={"owner":"$MY_UPN"}
AZ_NAME_PREFIX=$PREFIX
AZ_POLICY_EXEMPTIONS=n/a
TARKESH_UPN=$MY_UPN
ENTRA_APP_DISPLAY_NAME=$APP_REG_NAME
ENTRA_APP_CLIENT_ID=$CLIENT_ID
ENTRA_REDIRECT_URI_OWNER=$MY_UPN
ENTRA_GROUP_ADMIN=n/a
ENTRA_GROUP_ADMIN_NAME=n/a
ENTRA_GROUP_MEMBER=n/a
ENTRA_GROUP_MEMBER_NAME=n/a
ENTRA_GROUP_OWNERS=n/a
ENTRA_CA_NOTES=tenant default
ENTRA_TOKEN_LIFETIME=tenant default
ENTRA_CLOUD=public
DEPLOY_CLIENT_ID=n/a
GH_ENV_REVIEWERS=n/a
ACR_TASKS_NETWORK_PATH=n/a
CLOUDSHELL_STORAGE=ephemeral
CLOUDSHELL_VNET=n/a
SPOKE_VNET_CIDR=$VNET_CIDR
HUB_VNET_ID=n/a
HUB_DNS_IP=n/a
HUB_FIREWALL_IP=n/a
FW_RULE_COLLECTION=n/a
PDNS_ZONE_IDS=n/a
AMPLS_ID=n/a
REGISTRY_CHOICE=a
SHARED_ACR_LOGIN_SERVER=n/a
CITADEL_ENDPOINT=n/a
CITADEL_DEPLOYMENT=n/a
CITADEL_AUTH_MODE=n/a
CITADEL_APPROVAL_REF=n/a
APP_HOSTNAME=n/a
INGRESS_ENTRY=n/a
TLS_CERT_REF=n/a
LAW_ID=n/a
LOG_RETENTION_DAYS=30
ENTRA_SIGNIN_LOGS=n/a
ALERT_EMAILS=$ALERT_EMAIL
EOF
chmod 600 ~/inv-studio-azure/deploy-kit.env
echo "deploy kit written"
```

To let only your office reach the app, add a line such as `INGRESS_ALLOWED_CIDRS=203.0.113.0/24` to the kit.
With a hub network, firewall or a gateway from the platform team, fill those lines instead of `n/a`
(the comments in `infra/azure/deploy-kit.example.env` say what each one means).

## Step 6. Make the two secrets, check, and create the resources

**a. Make the two secrets.** Copy both into your password manager **now**. If you lose the vault key, saved
provider credentials can't be read again. Never paste either into a chat or a ticket.

```bash
python3 -c 'import secrets; print("Database admin password:", secrets.token_urlsafe(24))'
python3 -c 'import base64,os; print("Vault key:", base64.urlsafe_b64encode(os.urandom(32)).decode())'
```

**b. Preflight.** This creates nothing. It checks your sign-in, roles, providers, quota, the template and Azure
Policy. Fix what it names and run it again until every line passes.

```bash
cd ~/Invoice_studio_Al && ./scripts/cloudshell.sh check
```

**c. Create everything** (about 15 minutes; costs start here). It asks for the two secrets (paste them; nothing
shows as you paste), shows what it will create, and waits for you to type `DEPLOY`.

```bash
cd ~/Invoice_studio_Al && ./scripts/cloudshell.sh infra
source ~/inv-studio-azure/outputs.env && echo "App address: $APP_URL"
```

## Step 7. Add the app address to the sign-in app, and give yourself the vault

```bash
source ~/inv-studio-azure/me.env && source ~/inv-studio-azure/outputs.env
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications(appId='$CLIENT_ID')" \
  --headers Content-Type=application/json --body "{\"spa\":{\"redirectUris\":[\"$APP_URL/\"]}}"
cd ~/Invoice_studio_Al && ./scripts/cloudshell.sh vault-access
```

## Step 8. Build the image (40 to 90 minutes the first time)

```bash
cd ~/Invoice_studio_Al && source ~/inv-studio-azure/outputs.env && ./scripts/cloudshell.sh build
```

If Cloud Shell disconnects, the build **keeps running** inside Azure. Reconnect (the line at the top), then:

```bash
az acr task list-runs -r "$ACR" -o table --top 3
```

- `Running`: wait and check again.
- `Succeeded`: paste the block below. It saves the image name the way `build` would have.
- `Failed`: paste the `build` block again.

```bash
cd ~/Invoice_studio_Al && TAG=$(git rev-parse --short HEAD)
echo "$(az acr show -n "$ACR" --query loginServer -o tsv)/inv-studio@$(az acr repository show -n "$ACR" --image "inv-studio:$TAG" --query digest -o tsv)" > ~/inv-studio-azure/last-image
cat ~/inv-studio-azure/last-image
```

## Step 9. Put the image on the app

It asks for the same two secrets again, then waits for `DEPLOY`. It finishes when the new version is Healthy.

```bash
cd ~/Invoice_studio_Al && ./scripts/cloudshell.sh image
source ~/inv-studio-azure/outputs.env && curl -fsS "$APP_URL/health" && echo && echo "Open $APP_URL"
```

Open the address in your browser and sign in with Microsoft. You should see the app as an admin.

## Step 10. Move the app off the database admin login

**a. Make the app's own database password** and save it in your password manager:

```bash
python3 -c 'import secrets; print("App database password:", secrets.token_urlsafe(24))'
```

**b. Create the database role.** This opens a shell inside the app. Paste the two lines it shows, one at a time.
The first asks for the password from 10a twice:

```bash
cd ~/Invoice_studio_Al && ./scripts/cloudshell.sh db-role
```

Inside the container:

```bash
python scripts/azure_db_app_role.py create
exit
```

**c. Point the app at the new role.** Back in Cloud Shell. It asks for the same password, without showing it:

```bash
source ~/inv-studio-azure/outputs.env
read -rsp 'App database password: ' APP_DB_PASSWORD; echo
DB_URL="postgresql://invstudio_app:$(printf %s "$APP_DB_PASSWORD" | python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')@$DB_HOST:5432/invstudio?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt"
SECRET_FILE=$(mktemp); printf %s "$DB_URL" > "$SECRET_FILE"
az keyvault secret set --vault-name "$VAULT" --name inv-studio-database-url --file "$SECRET_FILE" --encoding utf-8 -o none
rm -f "$SECRET_FILE"; unset APP_DB_PASSWORD DB_URL SECRET_FILE
REV=$(az containerapp revision list -n "$APP" -g "$RG" --query "[?properties.active] | [-1].name" -o tsv)
az containerapp revision restart -n "$APP" -g "$RG" --revision "$REV"
echo "restarted $REV"
```

**d. Check it.** Open the shell again and run the check. It must print
`OK: certificate verified, least-privilege role, owns every app table`:

```bash
az containerapp exec -n "$APP" -g "$RG" --command bash
```

```bash
python scripts/azure_db_app_role.py check
exit
```

## Step 11. Close the public doors of Key Vault and Blob storage

It checks the private connections first. If anything fails, it opens them again by itself and tells you why.
When it asks for an admin token, get one as in Step 12 (or press Enter to skip that part).

```bash
cd ~/Invoice_studio_Al && ./scripts/cloudshell.sh lockdown
```

## Step 12. Test the deployment

You need your admin token from the browser:

1. Open the app address and sign in.
2. Press **F12**, open **Network**, and reload the page.
3. Click any request to `/api/...`. Under **Request Headers → Authorization**, copy the long text after `Bearer `.

The token lasts about an hour. Don't paste it anywhere but here.

```bash
cd ~/Invoice_studio_Al && ./scripts/cloudshell.sh test
```

Paste the token when asked. Every line must show a tick (✓). It uploads the synthetic sample invoice, reads it and
deletes it.

## Step 13. Check any time

```bash
cd ~/Invoice_studio_Al && ./scripts/cloudshell.sh status
```

This shows revision health and traffic, whether Key Vault and Blob are locked down, the alerts, and `/health`.

---

## After the first deploy

- **Give other people access.** Entra admin center → Enterprise applications → *Invoice Studio* → Users and
  groups → Add → choose the person (or a group, with Entra ID P1) and the role `admin` or `member`.
- **Switch Azure AI on** (optional; the app runs fully without it). Follow AZURE.md §7, and first prove the
  endpoint, key and deployment with `scripts/azure_ai_check.py`.
- **Later versions.** Don't run `infra` or `image` again on the running app. New images go out through
  GitHub **Actions → Deploy to Azure** (AZURE.md §4 and §8).
- **Roll back.** See AZURE.md §9.
