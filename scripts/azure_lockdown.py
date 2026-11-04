"""Close the public endpoints of Key Vault and Blob Storage, from Azure Cloud Shell, and prove the app still works.

    python3 scripts/azure_lockdown.py -g <resource group> --status    # what is open, what is private
    python3 scripts/azure_lockdown.py -g <resource group>             # check, close, restart, prove
    python3 scripts/azure_lockdown.py -g <resource group> --undo      # open them again (maintenance)

InfoSec LLD §9 asks for Key Vault and Blob to be private. main.bicep always creates their private endpoints
(privatelink.bicep), but leaves the public endpoints on for the first deploy: the Cloud Shell steps before
go-live (azure_deploy.py --image, AZURE.md §3, §5a) read the vault from outside the network. Run this once
those are done. It:

  1. checks both private endpoints exist, are Approved and have their private DNS record;
  2. sets publicNetworkAccess Disabled and the firewall default to Deny on the vault and the storage account;
  3. restarts the live revision, so Container Apps fetches the app's Key Vault secret references again, now
     through the private endpoint, and waits until it is Healthy and /health answers 200;
  4. with a signed-in token (INV_LOCKDOWN_TOKEN, or pasted at the hidden prompt), calls
     /api/health/dependencies, which reads the database and lists the Blob container through the endpoints.

If 3 or 4 fails, it opens both again at once and says why, so the app is never left broken. A later
`azure_deploy.py --rerun` sees the closed vault and keeps it closed. After the lockdown, a vault secret
is changed from a machine in the network, or with --undo, the change, then this script again.
"""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from azure_deploy import STATE, AzError, az  # noqa: E402

READY_TRIES = 60
READY_PAUSE = 20


class Stop(Exception):
    """One failing reason."""


def names(group, run):
    """The vault, storage account and app of the group: from azure_deploy.py's saved outputs, else discovered."""
    try:
        outputs = json.loads((STATE / "outputs.json").read_text())
        found = {k: outputs[k]["value"] for k in ("vaultName", "blobAccountName", "appName")}
        if found["vaultName"] and found["blobAccountName"] and found["appName"]:
            return found
    except (OSError, ValueError, KeyError, TypeError):
        pass
    found = {}
    for key, args in (("vaultName", ("keyvault", "list")), ("blobAccountName", ("storage", "account", "list")),
                      ("appName", ("containerapp", "list"))):
        items = run(*args, "-g", group, "--query", "[].name") or []
        if len(items) != 1:
            raise Stop(f"found {len(items)} results for `az {' '.join(args)}` in {group}, not one")
        found[key] = items[0]
    return found


def resource_id(kind, name, group, run):
    args = ("keyvault", "show", "-n", name) if kind == "vault" else ("storage", "account", "show", "-n", name)
    return run(*args, "-g", group, "--query", "id")


def check_endpoints(group, vault_id, storage_id, run):
    endpoints = run("network", "private-endpoint", "list", "-g", group) or []
    for label, target in (("Key Vault", vault_id), ("Blob", storage_id)):
        match = [e for e in endpoints
                 if any(c.get("privateLinkServiceId", "").lower() == target.lower()
                        for c in e.get("privateLinkServiceConnections") or [])]
        if not match:
            raise Stop(f"no private endpoint for {label} in {group}: run scripts/azure_deploy.py --rerun first, "
                       "so main.bicep creates privatelink.bicep")
        endpoint = match[0]
        connection = (endpoint.get("privateLinkServiceConnections") or [{}])[0]
        status = (connection.get("privateLinkServiceConnectionState") or {}).get("status", "")
        if status != "Approved":
            raise Stop(f"the {label} private endpoint {endpoint['name']} is {status or 'not approved'}, not Approved")
        zones = run("network", "private-endpoint", "dns-zone-group", "list", "-g", group,
                    "--endpoint-name", endpoint["name"]) or []
        if not zones:
            raise Stop(f"the {label} private endpoint {endpoint['name']} has no private DNS record: the app would "
                       "still resolve the public address. Pass the hub zone in PDNS_ZONE_IDS, or let main.bicep "
                       "create the zone, and run azure_deploy.py --rerun")
        print(f"  ok    {label} private endpoint {endpoint['name']}: Approved, DNS record registered")


def set_access(found, group, state, run):
    action = "Deny" if state == "Disabled" else "Allow"
    run("keyvault", "update", "-n", found["vaultName"], "-g", group, "--public-network-access", state,
        "--default-action", action, "--bypass", "AzureServices")
    run("storage", "account", "update", "-n", found["blobAccountName"], "-g", group, "--public-network-access", state,
        "--default-action", action, "--bypass", "AzureServices")
    print(f"  ok    Key Vault {found['vaultName']} and storage {found['blobAccountName']}: public access {state}")


def status(found, group, run):
    vault = run("keyvault", "show", "-n", found["vaultName"], "-g", group,
                "--query", "{access:properties.publicNetworkAccess,default:properties.networkAcls.defaultAction}")
    blob = run("storage", "account", "show", "-n", found["blobAccountName"], "-g", group,
               "--query", "{access:publicNetworkAccess,default:networkRuleSet.defaultAction}")
    print(f"  Key Vault {found['vaultName']}: public access {vault.get('access')}, default {vault.get('default')}")
    print(f"  Storage   {found['blobAccountName']}: public access {blob.get('access')}, default {blob.get('default')}")
    return vault.get("access") == "Disabled" and blob.get("access") == "Disabled"


def live_revision(app, group, run):
    revisions = run("containerapp", "revision", "list", "-n", app, "-g", group, "--query",
                    "[?properties.active].{name:name,traffic:properties.trafficWeight}") or []
    serving = sorted(revisions, key=lambda r: r.get("traffic") or 0, reverse=True)
    if not serving or not serving[0].get("traffic"):
        raise Stop(f"no revision of {app} has traffic")
    return serving[0]["name"]


def http_status(url, token=None):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as err:
        return err.code
    except (urllib.error.URLError, OSError):
        return None


def prove(found, group, token, run, fetch=http_status, sleep=time.sleep):
    app = found["appName"]
    revision = live_revision(app, group, run)
    url = "https://" + run("containerapp", "show", "-n", app, "-g", group,
                           "--query", "properties.configuration.ingress.fqdn")
    print(f"  ...   restarting {revision}, so its Key Vault references are fetched through the private endpoint")
    run("containerapp", "revision", "restart", "-n", app, "-g", group, "--revision", revision)
    health = "Unknown"
    for _ in range(READY_TRIES):
        sleep(READY_PAUSE)
        health = run("containerapp", "revision", "show", "-n", app, "-g", group, "--revision", revision,
                     "--query", "properties.healthState")
        if health == "Unhealthy":
            break
        if health == "Healthy" and fetch(url + "/health") == 200:
            print(f"  ok    {revision} is Healthy and {url}/health answers 200")
            break
    else:
        health = "not ready in time"
    if health != "Healthy":
        raise Stop(f"{revision} is {health} after the lockdown: its secret references or DNS do not resolve "
                   "through the private endpoint")
    if not token:
        print("  -     /api/health/dependencies not called (no token): the database and Blob were not proven.\n"
              f"        Sign in at {url}, copy a token (docs/API.md §2), and run this again; it is safe to repeat.")
        return
    code = fetch(url + "/api/health/dependencies", token)
    if code != 200:
        raise Stop(f"/api/health/dependencies answered {code}: the app cannot reach the database or Blob "
                   "through the private endpoints")
    print("  ok    /api/health/dependencies: database and Blob answer through the private endpoints")


def main(argv=None, run=az, read_token=None, fetch=http_status, sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-g", "--group", required=True, help="the resource group of the deployment")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="show public access of the vault and storage only")
    mode.add_argument("--undo", action="store_true", help="open the public endpoints again")
    args = parser.parse_args(argv)
    try:
        found = names(args.group, run)
        if args.status:
            closed = status(found, args.group, run)
            print("Locked down." if closed else "Not locked down.")
            return 0
        if args.undo:
            set_access(found, args.group, "Enabled", run)
            print("Public endpoints open again. Run this script without --undo to close them.")
            return 0
        print("Lockdown of Key Vault and Blob")
        vault_id = resource_id("vault", found["vaultName"], args.group, run)
        storage_id = resource_id("storage", found["blobAccountName"], args.group, run)
        check_endpoints(args.group, vault_id, storage_id, run)
        token = os.environ.get("INV_LOCKDOWN_TOKEN", "").strip()
        if not token:
            reader = read_token or (lambda: getpass.getpass("Signed-in token for /api/health/dependencies "
                                                            "(Enter to skip): ").strip())
            token = reader()
        set_access(found, args.group, "Disabled", run)
        try:
            prove(found, args.group, token, run, fetch=fetch, sleep=sleep)
        except (Stop, AzError) as err:
            print(f"\nFAILED: {err}\nOpening the public endpoints again so the app keeps working...", file=sys.stderr)
            set_access(found, args.group, "Enabled", run)
            prove(found, args.group, "", run, fetch=fetch, sleep=sleep)
            return 2
        print("\nLocked down: Key Vault and Blob answer only through their private endpoints.")
        return 0
    except Stop as stop:
        print(f"\nSTOP: {stop}", file=sys.stderr)
        return 1
    except AzError as err:
        print(f"\nSTOP: an az command failed: {str(err).splitlines()[0] if str(err) else err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
