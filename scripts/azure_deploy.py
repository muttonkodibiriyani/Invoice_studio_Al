"""The first Azure deploy of Invoice Studio, in one command, from Azure Cloud Shell (Bash).

    python3 scripts/azure_deploy.py --kit ~/inv-studio-azure/deploy-kit.env --check   # preflight only
    python3 scripts/azure_deploy.py --kit ~/inv-studio-azure/deploy-kit.env           # preflight, then deploy
    python3 scripts/azure_deploy.py --kit ~/inv-studio-azure/deploy-kit.env \
        --image <acr login server>/inv-studio@sha256:<digest>                         # put the real app on, once

The kit is infra/azure/deploy-kit.example.env filled in from the platform team's answer to the access
request. The preflight runs in this order and stops at the first check that fails, with that one reason,
before anything is created:

  kit        every placeholder filled, nothing missing or unknown; Entra in the public cloud
  ai         the AI endpoint is one the app accepts, with a deployment, managed identity and an egress rule; or n/a
  tools      az and Bicep present
  sign-in    signed in, in the kit's tenant and subscription, as TARKESH_UPN
  group      the resource group exists in AZ_REGION and is empty (--rerun and --image skip "empty")
  roles      Owner, or Contributor plus a role that can create role assignments for the template's three
  providers  every resource provider the template uses is Registered
  quota      PostgreSQL Standard_B2s offered in the region; no Container Apps quota already full
  template   main.bicep needs nothing the kit does not give
  policy     ARM validation and what-if, which is where Azure Policy refuses a deployment

Then it shows the what-if counts and asks you to type DEPLOY. --check runs the same preflight with
throwaway secrets that are never printed or stored, so it creates nothing and needs nothing from you.

The database admin password and the vault key are asked for hidden. They go to az in a parameter file
readable only by you, which is deleted when the script ends, and are never printed.

--image puts the first real image on the app, once, after the first deploy left it on the hello-world
image. It runs main.bicep again with containerImage set, so the template gives the revision port 8080 and
the /health startup probe; `az containerapp update --image` alone keeps the placeholder's port 80 and no
probe, and that revision never becomes healthy. It asks for no secret: it reads the database URL and the
vault key from the vault (AZURE.md §3 gives you read access), passes the admin password from that URL and
the same vault key, and writeSecrets false, so the database password and every vault secret stay as they
are. It refuses once AZURE.md §5a has moved the app off the admin login: from then on the Deploy to Azure
workflow puts images on.
"""
import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import azure_foundry  # noqa: E402  standard library only, so Cloud Shell runs it with no install

TEMPLATE = ROOT / "infra" / "azure" / "main.bicep"
STATE = Path.home() / "inv-studio-azure"
DEPLOYMENT_NAME = "invstudio-infra"
# main.bicep's postgresAdminLogin default; the script never passes it. The database URL logs in as it until §5a.
ADMIN_LOGIN = "invstudio"
DATABASE_URL_SECRET = "inv-studio-database-url"
VAULT_KEY_SECRET = "inv-studio-vault-key"
IMAGE_BY_DIGEST = re.compile(r"([a-z0-9][a-z0-9.-]*[a-z0-9](?::[0-9]+)?)/inv-studio@sha256:[0-9a-f]{64}")

# The placeholders of the access request (tm8 doc 01a187ad-7fd4, v4), in its order. The kit has exactly these.
KIT_KEYS = (
    "AZ_SUBSCRIPTION_ID", "AZ_TENANT_ID", "AZ_RESOURCE_GROUP", "AZ_REGION", "AZ_REQUIRED_TAGS",
    "AZ_NAME_PREFIX", "AZ_POLICY_EXEMPTIONS", "TARKESH_UPN",
    "ENTRA_APP_DISPLAY_NAME", "ENTRA_APP_CLIENT_ID", "ENTRA_REDIRECT_URI_OWNER", "ENTRA_GROUP_ADMIN",
    "ENTRA_GROUP_ADMIN_NAME", "ENTRA_GROUP_MEMBER", "ENTRA_GROUP_MEMBER_NAME", "ENTRA_GROUP_OWNERS", "ENTRA_CA_NOTES",
    "ENTRA_TOKEN_LIFETIME", "ENTRA_CLOUD",
    "DEPLOY_CLIENT_ID", "GH_ENV_REVIEWERS", "ACR_TASKS_NETWORK_PATH",
    "CLOUDSHELL_STORAGE", "CLOUDSHELL_VNET",
    "SPOKE_VNET_CIDR", "HUB_VNET_ID", "HUB_DNS_IP", "HUB_FIREWALL_IP", "FW_RULE_COLLECTION",
    "PDNS_ZONE_IDS", "AMPLS_ID", "REGISTRY_CHOICE", "SHARED_ACR_LOGIN_SERVER",
    "CITADEL_ENDPOINT", "CITADEL_DEPLOYMENT", "CITADEL_AUTH_MODE", "CITADEL_APPROVAL_REF",
    "APP_HOSTNAME", "INGRESS_ENTRY", "TLS_CERT_REF", "LAW_ID", "LOG_RETENTION_DAYS", "ENTRA_SIGNIN_LOGS",
)
# The app's sign-in authority is fixed at the public cloud (Entra design 01a187ad-16bc §3.1).
PUBLIC_CLOUD = {"public", "azurecloud", "login.microsoftonline.com", "https://login.microsoftonline.com"}
NOT_APPLICABLE = "n/a"
AI_EGRESS = re.compile(r"[A-Za-z][A-Za-z0-9.]{1,80}")  # a service tag, optionally with .region

# Kit value -> main.bicep parameter. A parameter main.bicep does not declare yet (the "(new)" lines of the
# access request) is skipped with a note, and passed once the Entra or target-round change adds it.
# CITADEL_ENDPOINT=n/a deploys with AI off; the other AI lines are then ignored. check_ai() guards the rest.
STRING = lambda v: v  # noqa: E731


def ip_address(value):
    return str(ipaddress.ip_address(value.strip()))


def cidr_list(value):
    return [str(ipaddress.ip_network(part.strip(), strict=False)) for part in value.split(",") if part.strip()]


def email_list(value):
    emails = [part.strip() for part in value.split(",") if part.strip()]
    if not emails or any(not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", e) for e in emails):
        raise ValueError(value)
    return emails


def boolean(value):
    text = value.strip().casefold()
    if text in ("true", "yes", "1", "on"):
        return True
    if text in ("false", "no", "0", "off"):
        return False
    raise ValueError(value)


def auth_mode(value):
    """The access request writes the Citadel auth mode in words; main.bicep takes key or managed_identity."""
    text = value.strip().casefold().replace("-", " ").replace("_", " ")
    if "managed" in text or text in ("mi", "entra", "entra id", "aad"):
        return "managed_identity"
    if "key" in text:
        return "key"
    raise ValueError(value)


PARAMS = {
    "AZ_REGION": ("location", STRING),
    "AZ_NAME_PREFIX": ("namePrefix", STRING),
    "AZ_REQUIRED_TAGS": ("tags", json.loads),
    "SPOKE_VNET_CIDR": ("networkAddressPrefix", STRING),
    "LOG_RETENTION_DAYS": ("logRetentionInDays", int),
    "APP_HOSTNAME": ("extraAllowedOrigins", lambda v: [f"https://{v}"]),
    "AZ_TENANT_ID": ("entraTenantId", STRING),
    "ENTRA_APP_CLIENT_ID": ("entraClientId", STRING),
    "ENTRA_GROUP_ADMIN_NAME": ("entraAdminGroupName", STRING),
    "ENTRA_GROUP_MEMBER_NAME": ("entraMemberGroupName", STRING),
    "HUB_VNET_ID": ("hubVnetId", STRING),
    "HUB_DNS_IP": ("dnsServers", lambda v: [ip.strip() for ip in v.split(",")]),
    "HUB_FIREWALL_IP": ("egressFirewallIp", ip_address),
    "PDNS_ZONE_IDS": ("privateDnsZoneIds", json.loads),
    "AMPLS_ID": ("amplsId", STRING),
    "CITADEL_ENDPOINT": ("foundryEndpoint", STRING),
    "CITADEL_DEPLOYMENT": ("foundryDeployment", STRING),
    "CITADEL_AUTH_MODE": ("foundryAuthMode", auth_mode),
    "LAW_ID": ("existingLogWorkspaceId", STRING),
    # Optional lines (InfoSec hardening). A kit without them still works: they are left at main.bicep's defaults.
    "ALERT_EMAILS": ("alertEmails", email_list),
    "INGRESS_ALLOWED_CIDRS": ("ingressAllowedCidrs", cidr_list),
    "RESTRICT_EGRESS": ("restrictEgress", boolean),
    # Optional lines (AI on, AZURE.md §7).
    "CITADEL_GATEWAY_HOST": ("foundryGatewayHost", str.casefold),
    "CITADEL_TOKEN_SCOPE": ("foundryTokenScope", STRING),
    "AI_EGRESS_DESTINATION": ("aiEgressDestination", STRING),
}
# Lines a kit may leave out: added after the access request, so an older filled-in kit stays valid.
OPTIONAL_KIT_KEYS = ("ALERT_EMAILS", "INGRESS_ALLOWED_CIDRS", "RESTRICT_EGRESS",
                     "CITADEL_GATEWAY_HOST", "CITADEL_TOKEN_SCOPE", "AI_EGRESS_DESTINATION")
# Meaningless without an endpoint: an AI-off kit may keep them filled, they are not passed.
AI_ONLY_KEYS = ("CITADEL_DEPLOYMENT", "CITADEL_GATEWAY_HOST", "CITADEL_TOKEN_SCOPE", "AI_EGRESS_DESTINATION")
# main.bicep's own default tags, kept when the platform's tags are added.
DEFAULT_TAGS = {"application": "inv-studio", "managedBy": "infra/azure/main.bicep"}

PROVIDERS = (
    "Microsoft.App", "Microsoft.ContainerRegistry", "Microsoft.DBforPostgreSQL", "Microsoft.KeyVault",
    "Microsoft.OperationalInsights", "Microsoft.Storage", "Microsoft.ManagedIdentity", "Microsoft.Network",
    "Microsoft.Insights", "Microsoft.CloudShell",
)
POSTGRES_SKU = "Standard_B2s"

# The roles the template assigns to the app identity, and the one Tarkesh gives himself on the vault (runbook A8).
TEMPLATE_ROLES = {
    "7f951dda-4ed3-4680-a7ca-43fe172d538d": "AcrPull",
    "ba92f5b4-2d11-453d-a403-e96b0029c9fe": "Storage Blob Data Contributor",
    "4633458b-17de-408a-b874-0445c86b69e6": "Key Vault Secrets User",
    "b86a8fe4-44ce-4948-aee5-eccb2c155cd7": "Key Vault Secrets Officer",
}
OWNER = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
RBAC_ADMIN = "f58310d9-a9f6-439a-9e8d-f62e7b41a168"
USER_ACCESS_ADMIN = "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9"

# Not checkable before the deploy: reported as not run, never as passed.
NOT_RUN = (
    "ACR Tasks allowed on the subscription (access request D3): shows on the first image build",
    "Entra tenant settings, consent and Conditional Access (E5, E6): shows at the first sign-in",
    "Hub firewall allow-list and routing (H3, H4): shows when the first revision starts",
    "With AI on, the app identity's Cognitive Services OpenAI User role on the Foundry resource (AZURE.md §7): "
    "shows at the first AI read",
)


class Stop(Exception):
    """One failing reason. Nothing has been created when it is raised."""


class AzError(Exception):
    """az exited non-zero; the message is its stderr."""


def az(*args):
    """Run az and return its JSON output. Raises AzError with az's own message when it fails."""
    result = subprocess.run(["az", *args, "--only-show-errors", "-o", "json"], capture_output=True, text=True)
    if result.returncode != 0:
        raise AzError(result.stderr.strip() or f"az {args[0]} failed")
    return json.loads(result.stdout) if result.stdout.strip() else None


def ok(message):
    print(f"  ok    {message}")


def read_kit(path):
    """KEY=value lines, '#' comments. Refuses unfilled, missing and unknown keys."""
    try:
        text = Path(path).read_text()
    except OSError as err:
        raise Stop(f"cannot read the deploy kit {path}: {err.strerror}") from None
    kit = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = re.sub(r"\s+#.*$", "", line).strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise Stop(f"deploy kit line {number} is not KEY=value")
        kit[key.strip()] = value.strip()
    unknown = [k for k in kit if k not in KIT_KEYS + OPTIONAL_KIT_KEYS]
    if unknown:
        raise Stop(f"deploy kit has a line the access request does not: {unknown[0]}")
    for key in KIT_KEYS:
        if key not in kit:
            raise Stop(f"deploy kit is missing {key}: copy the line from infra/azure/deploy-kit.example.env")
        if not kit[key] or re.search(r"<[A-Z0-9_]+>", kit[key]):
            raise Stop(f"deploy kit: {key} is not filled in yet (write n/a if it does not apply)")
    for key in OPTIONAL_KIT_KEYS:
        if key not in kit or not kit[key]:
            kit[key] = NOT_APPLICABLE
        elif re.search(r"<[A-Z0-9_]+>", kit[key]):
            raise Stop(f"deploy kit: {key} is not filled in yet (write n/a if it does not apply)")
    if kit["ENTRA_CLOUD"].lower().rstrip("/") not in PUBLIC_CLOUD:
        raise Stop(f"ENTRA_CLOUD is {kit['ENTRA_CLOUD']}, not the public cloud: the app signs in only at "
                   "login.microsoftonline.com, so a sovereign cloud needs an app change first (access request E8)")
    return kit


def check_ai(kit):
    """AI on or off, decided before anything is created. The origin to probe when AI is on, else None."""
    endpoint = kit["CITADEL_ENDPOINT"]
    if endpoint == NOT_APPLICABLE:
        print("  note  AI off on this deploy: CITADEL_ENDPOINT is n/a. AZURE.md §7 switches it on later")
        return None
    gateway = "" if kit["CITADEL_GATEWAY_HOST"] == NOT_APPLICABLE else kit["CITADEL_GATEWAY_HOST"].casefold()
    if gateway and not azure_foundry.valid_gateway(gateway):
        raise Stop("deploy kit: CITADEL_GATEWAY_HOST must be one bare host name, with no https://, path, port or address")
    try:
        origin = azure_foundry.normalise_endpoint(endpoint, gateway)
    except ValueError as err:
        host = (urllib.parse.urlsplit(endpoint if "://" in endpoint else "https://" + endpoint).hostname or "").casefold()
        hint = (" Name exactly this host in CITADEL_GATEWAY_HOST to use the gateway." if not gateway
                and host.endswith(".azure-api.net") else "")
        raise Stop(f"deploy kit: the app would refuse CITADEL_ENDPOINT: {err}.{hint} The endpoint must be the host "
                   "alone, or end in /openai or /openai/v1, or be a Foundry project (<resource>.services.ai.azure.com/api/projects/"
                   "<project>); a gateway under any other path needs its API moved. "
                   "Write n/a to deploy with AI off") from None
    if gateway and origin != "https://" + gateway:
        raise Stop(f"deploy kit: CITADEL_GATEWAY_HOST is {gateway}, but CITADEL_ENDPOINT is {origin}: "
                   "they must name the same host, or write n/a for the gateway")
    if not azure_foundry._DEPLOYMENT.fullmatch(kit["CITADEL_DEPLOYMENT"]) or kit["CITADEL_DEPLOYMENT"] == NOT_APPLICABLE:
        raise Stop("deploy kit: CITADEL_DEPLOYMENT must name the Foundry deployment, or the agent on a project endpoint, when CITADEL_ENDPOINT is set")
    try:
        mode = auth_mode(kit["CITADEL_AUTH_MODE"])
    except ValueError:
        raise Stop("deploy kit: CITADEL_AUTH_MODE is not in the form the comment on its line asks for") from None
    if mode != "managed_identity":
        raise Stop("deploy kit: an endpoint with key auth would report Foundry connected on the placeholder key. Use "
                   "CITADEL_AUTH_MODE=managed_identity, or write CITADEL_ENDPOINT=n/a and follow AZURE.md §7 for a key")
    scope = kit["CITADEL_TOKEN_SCOPE"]
    if scope != NOT_APPLICABLE and not azure_foundry._SCOPE.fullmatch(scope):
        raise Stop("deploy kit: CITADEL_TOKEN_SCOPE must look like api://<gateway app id>/.default, or write n/a")
    egress = kit["AI_EGRESS_DESTINATION"]
    if not (AI_EGRESS.fullmatch(egress) and egress != NOT_APPLICABLE or is_cidr(egress)):
        raise Stop("deploy kit: AI_EGRESS_DESTINATION must be the Foundry or gateway private endpoint CIDR, or a service "
                   "tag such as CognitiveServicesFrontend: without it the apps NSG blocks every AI call")
    ok(f"AI on: endpoint {origin} accepted by the app" + (", by the exact gateway host in the kit" if gateway else ""))
    return origin


def is_cidr(value):
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return True


def probe_endpoint(origin):
    """True when the endpoint answers at all over TLS: any HTTP status counts, no credential is sent."""
    try:
        urllib.request.urlopen(origin + "/openai/v1/models", timeout=10)  # noqa: S310  an https origin check_ai accepted
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False
    return True


def report_probe(origin, probe):
    if probe(origin):
        ok(f"{origin} answers from here (no credential sent)")
    else:
        print(f"  note  {origin} did not answer from here. Not counted as passed: a private endpoint is reachable only "
              "from the app's network, so prove it from the running revision (AZURE.md §7)")


def data_plane_access(kit, run):
    """Disabled once scripts/azure_lockdown.py closed the vault, so a re-run never opens it again; else None."""
    try:
        states = run("keyvault", "list", "-g", kit["AZ_RESOURCE_GROUP"], "--query", "[].properties.publicNetworkAccess")
    except AzError:
        return None
    return "Disabled" if any(str(s).lower() == "disabled" for s in states or []) else None


def parameters(kit, declared, pg_password, vault_key, image=""):
    """The ARM parameter values for this kit, limited to what main.bicep declares, and the skipped names.

    With an image, writeSecrets is false: the vault keeps every secret it holds, and pg_password and
    vault_key must be the ones already in use."""
    values = {"postgresAdminPassword": pg_password, "vaultKey": vault_key, "containerImage": image,
              "writeSecrets": not image}
    skipped = []
    for key, (name, convert) in PARAMS.items():
        if kit[key] == NOT_APPLICABLE or (key in AI_ONLY_KEYS and kit["CITADEL_ENDPOINT"] == NOT_APPLICABLE):
            continue
        if name not in declared:
            skipped.append(f"{key} -> {name}")
            continue
        try:
            values[name] = convert(kit[key])
        except ValueError:
            raise Stop(f"deploy kit: {key} is not in the form the comment on its line asks for") from None
    if "tags" in values:
        if not isinstance(values["tags"], dict):
            raise Stop("deploy kit: AZ_REQUIRED_TAGS must be a JSON object")
        values["tags"] = {**DEFAULT_TAGS, **values["tags"]}
    if not re.fullmatch(r"[a-z0-9]{3,9}", kit["AZ_NAME_PREFIX"]):
        raise Stop("deploy kit: AZ_NAME_PREFIX must be 3-9 lower-case letters and digits (main.bicep namePrefix)")
    return values, skipped


def check_sign_in(kit, run):
    try:
        account = run("account", "show")
    except AzError:
        raise Stop("not signed in to Azure: run az login (Cloud Shell signs you in when it opens)") from None
    if account.get("tenantId", "").lower() != kit["AZ_TENANT_ID"].lower():
        raise Stop(f"signed in to tenant {account.get('tenantId')}, not AZ_TENANT_ID {kit['AZ_TENANT_ID']}")
    if account.get("id", "").lower() != kit["AZ_SUBSCRIPTION_ID"].lower():
        raise Stop(f"the selected subscription is {account.get('id')}, not AZ_SUBSCRIPTION_ID: "
                   f"run az account set --subscription {kit['AZ_SUBSCRIPTION_ID']}")
    try:
        user = run("ad", "signed-in-user", "show")
    except AzError:
        raise Stop("signed in, but not as a user (az ad signed-in-user show failed): sign in as yourself") from None
    if user.get("userPrincipalName", "").lower() != kit["TARKESH_UPN"].lower():
        raise Stop(f"signed in as {user.get('userPrincipalName')}, not TARKESH_UPN {kit['TARKESH_UPN']}")
    ok(f"signed in as {user['userPrincipalName']} to subscription {account['id']}")
    return user["id"]


def check_group(kit, run, rerun):
    """rerun is true for --rerun and --image: the group may hold resources."""
    group_name = kit["AZ_RESOURCE_GROUP"]
    try:
        group = run("group", "show", "-n", group_name)
    except AzError:
        raise Stop(f"resource group {group_name} does not exist, or you cannot read it (access request A2, B2)") \
            from None
    if group["location"].lower() != kit["AZ_REGION"].lower():
        raise Stop(f"resource group {group_name} is in {group['location']}, not AZ_REGION {kit['AZ_REGION']}")
    if not rerun:
        resources = run("resource", "list", "-g", group_name)
        apps = [r["name"] for r in resources if r.get("type", "").lower() == "microsoft.app/containerapps"]
        if apps:
            raise Stop(f"resource group {group_name} already holds the app {apps[0]}: the first deploy is done. "
                       "To put the real image on it, run with --image <server>/inv-studio@sha256:<digest> "
                       "(AZURE.md §5); after a deploy that failed part-way, run with --rerun")
        if resources:
            raise Stop(f"resource group {group_name} is not empty ({len(resources)} resources, e.g. "
                       f"{resources[0]['name']}): this is the first deploy. After a failed first attempt use --rerun")
    ok(f"resource group {group_name} in {group['location']}" + ("" if rerun else ", empty"))
    return group["id"]


def check_image(kit, image, run):
    """The image is named by digest and lives in the group's own registry, the one the app pulls from."""
    match = IMAGE_BY_DIGEST.fullmatch(image)
    if not match:
        raise Stop(f"--image {image} is not <acr login server>/inv-studio@sha256:<64 hex digits>: deploy by digest, "
                   "never by tag (AZURE.md §5 shows how to read the digest)")
    servers = [s.lower() for s in run("acr", "list", "-g", kit["AZ_RESOURCE_GROUP"], "--query", "[].loginServer") or []]
    if match.group(1) not in servers:
        raise Stop(f"--image is in {match.group(1)}, not in the registry of resource group "
                   f"{kit['AZ_RESOURCE_GROUP']} ({', '.join(servers) or 'none'}), the only one the app can pull from")
    ok(f"image by digest, in {match.group(1)}")


def find_vault(kit, run):
    """The vault the first deploy made: from its saved outputs, else the one Key Vault in the group."""
    try:
        return json.loads((STATE / "outputs.json").read_text())["vaultName"]["value"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    names = run("keyvault", "list", "-g", kit["AZ_RESOURCE_GROUP"], "--query", "[].name") or []
    if len(names) != 1:
        raise Stop(f"found {len(names)} Key Vaults in resource group {kit['AZ_RESOURCE_GROUP']}, not one: "
                   "--image is for a group the first deploy has made (run it without --image first)")
    return names[0]


def read_secrets(kit, run):
    """The admin password and vault key in use now, read from the vault. Never asked for, never printed."""
    vault = find_vault(kit, run)
    values = {}
    for name in (DATABASE_URL_SECRET, VAULT_KEY_SECRET):
        try:
            values[name] = run("keyvault", "secret", "show", "--vault-name", vault, "-n", name, "--query", "value")
        except AzError:
            raise Stop(f"cannot read {name} from Key Vault {vault}: give yourself Key Vault Secrets Officer on it "
                       "first (AZURE.md §3), then run this again") from None
        if not isinstance(values[name], str) or not values[name]:
            raise Stop(f"{name} in Key Vault {vault} is empty: do not re-run the template, ask for help")
    url = urllib.parse.urlsplit(values[DATABASE_URL_SECRET])
    user = urllib.parse.unquote(url.username or "")
    if user != ADMIN_LOGIN:
        raise Stop(f"the app's database URL logs in as {user or 'nobody'}, not the admin {ADMIN_LOGIN}: AZURE.md §5a "
                   "is done, so the real app is already on. From now on the Deploy to Azure workflow puts images on "
                   "(AZURE.md §5); do not re-run the template")
    password = urllib.parse.unquote(url.password or "")
    if len(password) < 12:
        raise Stop(f"the database URL in Key Vault {vault} has no admin password of 12 or more characters: "
                   "do not re-run the template, ask for help")
    key = values[VAULT_KEY_SECRET]
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}=", key):
        raise Stop(f"{VAULT_KEY_SECRET} in Key Vault {vault} is not a Fernet key: do not re-run the template, ask for help")
    ok(f"database admin password and vault key read from Key Vault {vault}; neither will change")
    return password, key


def check_roles(object_id, scope, run):
    assignments = run("role", "assignment", "list", "--assignee", object_id, "--scope", scope,
                      "--include-inherited", "--include-groups")
    by_role = {}
    for a in assignments:
        by_role.setdefault(a["roleDefinitionId"].rsplit("/", 1)[-1].lower(), []).append(a)
    pim = " If you use PIM, activate the eligible roles first."
    if OWNER in by_role:
        ok("roles: Owner on the resource group")
        return
    if CONTRIBUTOR not in by_role:
        raise Stop("you do not have Contributor (or Owner) on the resource group (access request B2)." + pim)
    granting = by_role.get(USER_ACCESS_ADMIN, []) + by_role.get(RBAC_ADMIN, [])
    if not granting:
        raise Stop("you cannot create role assignments: Role Based Access Control Administrator is missing on "
                   "the resource group, and the template creates three (access request B3)." + pim)
    if any(not a.get("condition") for a in granting):
        ok("roles: Contributor, and role assignments without a condition")
        return
    conditions = " ".join(a["condition"].lower() for a in granting)
    missing = [name for guid, name in TEMPLATE_ROLES.items() if guid not in conditions]
    if missing:
        raise Stop(f"your role-assignment condition does not allow {missing[0]}, which the deploy assigns "
                   "(access request B3)")
    ok("roles: Contributor, and RBAC Administrator conditioned to the four roles the deploy assigns")


def check_providers(run):
    for namespace in PROVIDERS:
        state = run("provider", "show", "-n", namespace, "--query", "registrationState")
        if state != "Registered":
            raise Stop(f"resource provider {namespace} is {state}, not Registered: the platform registers it "
                       "(access request C)")
    ok(f"{len(PROVIDERS)} resource providers registered")


def check_quota(kit, run):
    region = kit["AZ_REGION"]
    skus = run("postgres", "flexible-server", "list-skus", "--location", region)
    if POSTGRES_SKU not in json.dumps(skus):
        raise Stop(f"PostgreSQL Flexible Server {POSTGRES_SKU} is not offered to this subscription in {region} "
                   "(access request D2)")
    url = (f"https://management.azure.com/subscriptions/{kit['AZ_SUBSCRIPTION_ID']}/providers/Microsoft.App/"
           f"locations/{region}/usages?api-version=2024-03-01")
    for usage in (run("rest", "--method", "get", "--url", url) or {}).get("value", []):
        limit, current = usage.get("limit", 0), usage.get("currentValue", 0)
        if limit and current >= limit:
            name = usage.get("name", {}).get("localizedValue") or usage.get("name", {}).get("value")
            raise Stop(f"Container Apps quota '{name}' is full in {region} ({current} of {limit}): "
                       "ask the platform for more (access request D1)")
    ok(f"PostgreSQL {POSTGRES_SKU} offered in {region}; no Container Apps quota full")


def declared_parameters(run):
    """main.bicep's parameters, from the compiled template: {name: required?}."""
    compiled = run("bicep", "build", "--file", str(TEMPLATE), "--stdout")
    return {name: "defaultValue" not in spec for name, spec in compiled["parameters"].items()}


def check_template(declared, values, skipped):
    missing = [name for name, required in declared.items() if required and name not in values]
    if missing:
        raise Stop(f"main.bicep requires {missing[0]}, which the deploy kit does not give. Map it from a kit "
                   "line in PARAMS (scripts/azure_deploy.py) or give it a default in main.bicep")
    ok(f"template takes the kit ({len(values)} parameters)")
    for line in skipped:
        print(f"  note  not passed yet, main.bicep has no such parameter: {line}")


def check_policy(group_name, params_file, run):
    """Validation runs Azure Policy and the template's own checks; what-if lists what would change."""
    common = ["-g", group_name, "-f", str(TEMPLATE), "-p", f"@{params_file}"]
    try:
        run("deployment", "group", "validate", *common)
        result = run("deployment", "group", "what-if", *common, "--result-format", "ResourceIdOnly",
                     "--no-pretty-print")
    except AzError as err:
        message = str(err)
        if "RequestDisallowedByPolicy" in message:
            raise Stop("Azure Policy refuses the deployment (access request A5): "
                       + _first_line(message)) from None
        raise Stop("ARM refuses the deployment: " + _first_line(message)) from None
    if result.get("error"):
        raise Stop("what-if failed: " + json.dumps(result["error"])[:500])
    counts = {}
    for change in result.get("changes", []):
        counts[change["changeType"]] = counts.get(change["changeType"], 0) + 1
    ok("policy and validation passed; what-if: "
       + ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items())))
    return counts


def _first_line(message):
    return next((line.strip() for line in message.splitlines() if line.strip()), message)[:500]


def ask_secrets():
    print("\nThe two secrets from the password manager (runbook: make them first). They are not shown.")
    pg = getpass.getpass("Database admin password: ")
    if len(pg) < 12:
        raise Stop("the database admin password must be at least 12 characters (main.bicep)")
    key = getpass.getpass("Vault key: ")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}=", key):
        raise Stop("the vault key is not a Fernet key (44 characters of url-safe base64)")
    return pg, key


def write_params(values):
    """The ARM parameter file, readable only by you. The caller deletes it."""
    fd, path = tempfile.mkstemp(prefix="inv-studio-params-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                   "contentVersion": "1.0.0.0",
                   "parameters": {k: {"value": v} for k, v in values.items()}}, f)
    return path


def save_outputs(group_name, outputs):
    STATE.mkdir(mode=0o700, exist_ok=True)
    (STATE / "outputs.json").write_text(json.dumps(outputs, indent=1))
    names = {"APP": "appName", "ACR": "acrName", "VAULT": "vaultName", "APP_URL": "appUrl",
             "STORAGE": "blobAccountName", "DB_HOST": "databaseHost", "DB_SERVER": "databaseServerName"}
    lines = [f"export RG={group_name}"] + [f"export {env}={outputs[key]['value']}" for env, key in names.items()]
    (STATE / "outputs.env").write_text("\n".join(lines) + "\n")
    return STATE / "outputs.env"


def main(argv=None, run=az, ask=ask_secrets, confirm=input, probe=probe_endpoint):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kit", required=True, help="the filled deploy kit, e.g. ~/inv-studio-azure/deploy-kit.env")
    parser.add_argument("--check", action="store_true", help="preflight only, with throwaway secrets")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--rerun", action="store_true", help="the group is not empty: a first attempt failed")
    modes.add_argument("--image", metavar="SERVER/inv-studio@sha256:DIGEST",
                       help="put the first real image on the app, keeping every secret (AZURE.md §5)")
    args = parser.parse_args(argv)
    params_file = None
    try:
        print("Preflight")
        kit = read_kit(Path(args.kit).expanduser())
        ok("deploy kit: every access-request line filled in")
        ai_origin = check_ai(kit)
        if run is az and not shutil.which("az"):
            raise Stop("az is not installed: run this in Azure Cloud Shell (Bash)")
        try:
            declared = declared_parameters(run)
        except AzError as err:
            raise Stop("Bicep cannot build main.bicep: " + _first_line(str(err))) from None
        ok("az and Bicep present, main.bicep builds")
        object_id = check_sign_in(kit, run)
        scope = check_group(kit, run, args.rerun or bool(args.image))
        check_roles(object_id, scope, run)
        check_providers(run)
        check_quota(kit, run)
        if args.image:
            check_image(kit, args.image, run)
            pg, key = read_secrets(kit, run)
        elif args.check:
            pg, key = secrets.token_urlsafe(24), secrets.token_urlsafe(32)[:43] + "="
        else:
            pg, key = ask()
        values, skipped = parameters(kit, declared, pg, key, args.image or "")
        if args.rerun and "dataPlanePublicAccess" in declared and data_plane_access(kit, run):
            values["dataPlanePublicAccess"] = "Disabled"
            ok("Key Vault and Blob are locked down (scripts/azure_lockdown.py): the re-run keeps them private")
        check_template(declared, values, skipped)
        params_file = write_params(values)
        check_policy(kit["AZ_RESOURCE_GROUP"], params_file, run)
        if ai_origin:
            report_probe(ai_origin, probe)
        print("\nNot checked before the deploy (not counted as passed):")
        for line in NOT_RUN:
            print(f"  -     {line}")
        if args.check:
            print("\nPreflight passed. Nothing was created. Run again without --check to deploy.")
            return 0
        if args.image:
            prompt = (f"\nType DEPLOY to put {args.image} on the app. The template runs again with the same secrets; "
                      "the new revision takes the traffic once it is healthy (up to about 15 minutes): ")
        else:
            prompt = "\nType DEPLOY to create these resources (about 15 minutes; costs start now): "
        if confirm(prompt) != "DEPLOY":
            raise Stop("not confirmed; nothing was created")
        print("Deploying...")
        try:
            run("deployment", "group", "create", "-n", DEPLOYMENT_NAME, "-g", kit["AZ_RESOURCE_GROUP"],
                "-f", str(TEMPLATE), "-p", f"@{params_file}")
            outputs = run("deployment", "group", "show", "-n", DEPLOYMENT_NAME, "-g", kit["AZ_RESOURCE_GROUP"],
                          "--query", "properties.outputs")
        except AzError as err:
            print(f"\nFAILED: the deployment stopped: {_first_line(str(err))}\n"
                  f"Read it with: az deployment operation group list -n {DEPLOYMENT_NAME} -g {kit['AZ_RESOURCE_GROUP']}"
                  " --query \"[?properties.provisioningState=='Failed']\"\nFix it, then run again with "
                  + ("the same --image." if args.image else "--rerun."), file=sys.stderr)
            return 2
        saved = save_outputs(kit["AZ_RESOURCE_GROUP"], outputs)
        if args.image:
            app = outputs["appName"]["value"]
            print(f"\nThe real app is on {outputs['appUrl']['value']}. Wait until the newest revision is Healthy:\n"
                  f"  az containerapp revision list -n {app} -g {kit['AZ_RESOURCE_GROUP']} -o table --query "
                  "\"[].{revision:name,health:properties.healthState,traffic:properties.trafficWeight}\"\n"
                  "Then the smoke test and §5a, §8 in docs/AZURE.md. From now on the Deploy to Azure workflow "
                  "puts images on; do not run this script again.")
            return 0
        print(f"\nDeployed. The names it made are in {saved}: source it.\nApp address: {outputs['appUrl']['value']}\n"
              f"Next: add {outputs['appUrl']['value']}/ as an SPA redirect URI on the Entra app "
              f"{kit['ENTRA_APP_CLIENT_ID']} (runbook A6a; or ask {kit['ENTRA_REDIRECT_URI_OWNER']})")
        return 0
    except Stop as stop:
        print(f"\nSTOP: {stop}", file=sys.stderr)
        return 1
    except AzError as err:
        print(f"\nSTOP: an az command failed: {_first_line(str(err))}", file=sys.stderr)
        return 1
    finally:
        if params_file:
            os.unlink(params_file)


if __name__ == "__main__":
    sys.exit(main())
