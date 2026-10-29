"""Static checks on the Azure hosting assets.

`az bicep build` is the real compiler and the pipeline should run it, but it is not installed
everywhere. These checks catch what actually goes wrong when the templates are edited by hand:
a renamed module parameter, an output that no longer exists, an environment variable the
application needs going missing, and a deploy workflow that could fire on its own.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra" / "azure"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-azure.yml"
DOCS = ROOT / "docs" / "AZURE.md"
APP = ROOT / "app"

# What app/cloud_store.py, app/authentication.py and app/main.py read in cloud mode; the container
# app must pass all of it. Without the Entra tenant and client ids the app refuses to start.
REQUIRED_ENV = (
    "INV_STUDIO_ENTRA_TENANT_ID",
    "INV_STUDIO_ENTRA_CLIENT_ID",
    "INV_STUDIO_ENTRA_ADMIN_GROUP_NAME",
    "INV_STUDIO_ENTRA_MEMBER_GROUP_NAME",
    "INV_STUDIO_CLOUD",
    "INV_STUDIO_DATA",
    "INV_STUDIO_DATABASE_URL",
    "INV_STUDIO_VAULT_KEY",
    "INV_STUDIO_BUCKET",
    "INV_STUDIO_STORAGE_BACKEND",
    "INV_STUDIO_AZURE_STORAGE_ACCOUNT",
    "INV_STUDIO_ALLOWED_ORIGINS",
    "INV_STUDIO_ALLOWED_HOSTS",
)


def bicep_files():
    return sorted(INFRA.glob("*.bicep"))


def declared_params(text):
    """Parameter name -> whether it has a default."""
    found = {}
    for name, default in re.findall(r"^param (\w+) \w+(?:\[\])?( = .*)?$", text, re.MULTILINE):
        found[name] = bool(default)
    return found


def declared_outputs(text):
    return set(re.findall(r"^output (\w+) ", text, re.MULTILINE))


def module_blocks(text):
    """Each `module <symbol> '<path>' = { ... }` as (symbol, path, body)."""
    for match in re.finditer(r"^module (\w+) '([^']+)' = \{$", text, re.MULTILINE):
        depth, start = 0, match.end() - 1
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    yield match.group(1), match.group(2), text[start:index]
                    break


def passed_params(body):
    inside = re.search(r"\n  params: \{\n(.*?)\n  \}", body, re.DOTALL)
    assert inside, "a module needs a params block"
    return set(re.findall(r"^    (\w+):", inside.group(1), re.MULTILINE))


def test_the_templates_are_there():
    assert {path.name for path in bicep_files()} >= {
        "main.bicep", "identity.bicep", "registry.bicep", "storage.bicep",
        "postgres.bicep", "keyvault.bicep", "monitoring.bicep", "containerapp.bicep", "network.bicep",
    }


@pytest.mark.parametrize("path", bicep_files(), ids=lambda path: path.name)
def test_brackets_balance(path):
    text = path.read_text()
    assert "\t" not in text, "bicep is indented with spaces"
    for opening, closing in (("{", "}"), ("[", "]"), ("(", ")")):
        # Comments and string literals hold stray brackets, so count only outside them.
        # Strings first: a comment marker inside one (https://...) is not a comment.
        stripped = re.sub(r"//[^\n]*", "", re.sub(r"'[^'\n]*'", "''", text))
        assert stripped.count(opening) == stripped.count(closing), f"unbalanced {opening}{closing}"


@pytest.mark.parametrize("path", bicep_files(), ids=lambda path: path.name)
def test_names_are_declared_once(path):
    # Bicep keeps parameters, variables, resources, modules and outputs in one namespace, so an
    # output cannot be called `name` in a file that already has a `name` parameter.
    declarations = re.findall(r"^(?:param|var|output|resource|module) (\w+)", path.read_text(), re.MULTILINE)
    repeated = {name for name in declarations if declarations.count(name) > 1}
    assert not repeated, f"{path.name} declares {sorted(repeated)} more than once"


@pytest.mark.parametrize("path", bicep_files(), ids=lambda path: path.name)
def test_every_declared_parameter_is_used(path):
    text = path.read_text()
    for name in declared_params(text):
        body = re.sub(rf"^param {name} .*$", "", text, flags=re.MULTILINE)
        assert re.search(rf"\b{name}\b", body), f"{path.name} declares {name} and never uses it"


def test_modules_are_called_with_the_parameters_they_declare():
    main = (INFRA / "main.bicep").read_text()
    seen = 0
    for symbol, relative, body in module_blocks(main):
        module = INFRA / relative
        assert module.is_file(), f"module {relative} is missing"
        declared = declared_params(module.read_text())
        passed = passed_params(body)
        unknown = passed - set(declared)
        assert not unknown, f"{symbol} passes parameters {relative} does not declare: {sorted(unknown)}"
        required = {name for name, has_default in declared.items() if not has_default}
        assert not required - passed, f"{symbol} is missing required parameters: {sorted(required - passed)}"
        seen += 1
    assert seen == 8


def test_module_outputs_exist():
    main = (INFRA / "main.bicep").read_text()
    paths = {symbol: relative for symbol, relative, _ in module_blocks(main)}
    for symbol, name in re.findall(r"\b(\w+)\.outputs\.(\w+)\b", main):
        assert symbol in paths, f"{symbol}.outputs.{name} names no module"
        outputs = declared_outputs((INFRA / paths[symbol]).read_text())
        assert name in outputs, f"{paths[symbol]} has no output {name}"


def test_no_deployment_output_can_leak_a_secret():
    for path in bicep_files():
        text = path.read_text()
        secure = {
            name for name in declared_params(text)
            if re.search(rf"@secure\(\)\s*(?:@\w+\([^)]*\)\s*)*param {name} ", text)
        }
        for output in re.findall(r"^output \w+ \w+ = (.*)$", text, re.MULTILINE):
            leaked = {name for name in secure if re.search(rf"\b{name}\b", output)}
            assert not leaked, f"{path.name} outputs the secure parameter(s) {sorted(leaked)}"
        assert "@secure" not in "".join(re.findall(r"^output .*$", text, re.MULTILINE))


def test_the_database_url_is_never_a_deployment_output():
    # It carries the PostgreSQL password; it only ever goes into Key Vault.
    for path in bicep_files():
        for output in re.findall(r"^output \w+ \w+ = (.*)$", path.read_text(), re.MULTILINE):
            assert "postgresql://" not in output
            assert "databaseUrl" not in output


def test_the_container_app_passes_every_setting_the_app_reads():
    text = (INFRA / "containerapp.bicep").read_text()
    for name in REQUIRED_ENV:
        assert f"'{name}'" in text, f"the container app never sets {name}"
    assert "'azure'" in text, "the storage backend must be set to azure"


def test_every_setting_the_container_app_passes_is_one_something_reads():
    # A variable nobody reads is a setting that silently does nothing, as the Foundry key once did
    # under a name the app never looked up. AZURE_CLIENT_ID is read by azure-identity, not by app/.
    read_by_libraries = {"AZURE_CLIENT_ID"}
    source = "".join(path.read_text() for path in APP.rglob("*.py"))
    passed = set(re.findall(r"name: '([A-Z][A-Z0-9_]+)'", (INFRA / "containerapp.bicep").read_text()))
    assert passed
    unread = {name for name in passed - read_by_libraries if f'"{name}"' not in source}
    assert not unread, f"the container app sets {sorted(unread)}, which nothing in app/ reads"


def test_the_template_ships_no_google_credential():
    # Sign-in is Entra ID only, and the API holds no credential for it: it checks access tokens against
    # the tenant's public signing keys. No Google or Entra secret is stored, mounted or pointed at.
    for path in [*bicep_files(), INFRA / "main.parameters.example.json"]:
        text = path.read_text()
        for marker in ("GOOGLE_APPLICATION_CREDENTIALS", "firebase-key", "firebaseServiceAccount",
                       "service_account", "private_key", "storageType: 'Secret'", "clientSecret",
                       "client-secret", "ENTRA_CLIENT_SECRET"):
            assert marker not in text, f"{path.name} carries a credential ({marker})"


# Firebase sign-in left the Azure build (Entra ID sign-in design, section 5.4). A leftover parameter or
# setting would look as if it does something; the app refuses to start on some of them in cloud mode.
FIREBASE_MARKERS = (
    "googleCloudProject", "firebaseWebApiKey", "firebaseAppId", "firebaseAuthDomain",
    "bootstrapAdminEmails", "bootstrapMemberEmails", "GOOGLE_CLOUD_PROJECT", "INV_STUDIO_FIREBASE_",
    "INV_STUDIO_IDENTITY_ADMIN", "INV_STUDIO_PUBLIC_URL", "INV_STUDIO_ALLOWED_EMAILS",
    "INV_STUDIO_MEMBER_EMAILS", "INV_STUDIO_AUTH_TENANT_ID", "identitytoolkit", "securetoken",
)


def test_no_firebase_setting_is_left_in_the_deploy_files():
    for path in [*bicep_files(), INFRA / "main.parameters.example.json"]:
        text = path.read_text()
        for marker in FIREBASE_MARKERS:
            assert marker not in text, f"{path.name} still carries the Firebase setting {marker}"
        assert "firebase" not in text.lower(), f"{path.name} still mentions Firebase"


def test_the_entra_ids_are_required_and_passed_through():
    main = (INFRA / "main.bicep").read_text()
    params = declared_params(main)
    for name in ("entraTenantId", "entraClientId"):
        assert params.get(name) is False, f"{name} must be a required parameter with no default"
        # A GUID is 36 characters; common, organizations and consumers are refused at deploy time too.
        assert re.search(rf"@minLength\(36\)\s+@maxLength\(36\)\s+param {name} string", main), name
    assert params.get("entraAdminGroupName") is True and params.get("entraMemberGroupName") is True
    app = (INFRA / "containerapp.bicep").read_text()
    assert re.search(r"name: 'INV_STUDIO_ENTRA_TENANT_ID'\s+value: entraTenantId", app)
    assert re.search(r"name: 'INV_STUDIO_ENTRA_CLIENT_ID'\s+value: entraClientId", app)
    example = (INFRA / "main.parameters.example.json").read_text()
    assert '"<AZ_TENANT_ID>"' in example and '"<ENTRA_APP_CLIENT_ID>"' in example


def test_the_workflow_checks_the_entra_settings_before_it_builds():
    # The workflow reads the Entra values and never sets them: a second place that writes app settings
    # would drift from the Bicep deployment.
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
    names = [step.get("name", "") for step in steps]
    check = names.index("Check the Entra sign-in settings")
    assert check < names.index("Build the image in ACR")
    for var in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_ADMIN_GROUP_NAME", "ENTRA_MEMBER_GROUP_NAME"):
        assert steps[check]["env"][var] == "${{ vars.%s }}" % var
    body = WORKFLOW.read_text()
    assert "--set-env-vars" not in body and "--replace-env-vars" not in body


def test_after_the_warm_up_the_models_are_read_only_and_the_model_hub_is_off():
    # The warm-up downloads the models, so both steps must come after it (N6, N8). The app-only release builds on
    # an older image, so it repeats them; the Azure template sets the variable as well.
    dockerfile = (ROOT / "Dockerfile").read_text()
    head, tail = dockerfile.split("RUN python scripts/warm_ocr.py\n", 1)
    app_layer = (ROOT / "Dockerfile.app-layer").read_text()
    assert "HF_HUB_OFFLINE" not in head, "the warm-up must be able to download the models"
    for text in (tail, app_layer):
        assert re.search(r"^ENV HF_HUB_OFFLINE=1$", text, re.MULTILINE)
        lock = re.search(r'^\s+for d in (.*); do \\\n\s+test -d "\$d"; chown -R root:root "\$d"; chmod -R a-w "\$d";',
                         text, re.MULTILINE)
        assert lock, "the model folders are not made read-only"
        assert lock.group(1).split() == ["/home/studio/.cache/huggingface", "/home/studio/.paddlex/official_models",
                                         '"$rapidocr"']
        assert text.index("USER 1000:1000") > lock.end(), "the app must run as its own account again"
    bicep = (INFRA / "containerapp.bicep").read_text()
    assert re.search(r"name: 'HF_HUB_OFFLINE'\s+value: '1'", bicep)


def test_the_startup_probe_sends_a_host_the_app_accepts():
    # The app answers an unknown Host with 400; a probe's default Host is the replica's IP.
    text = (INFRA / "containerapp.bicep").read_text()
    probe = text.split("var startupProbe", 1)[1].split("\nvar ", 1)[0]
    assert re.search(r"name: 'Host'\s+value: appHost", probe), "the startup probe must send the app's hostname"
    assert re.search(r"name: 'INV_STUDIO_ALLOWED_HOSTS'\s+value: '\*\.\$\{environment\.properties\.defaultDomain\}'", text)


def test_the_database_has_no_public_endpoint():
    text = (INFRA / "postgres.bicep").read_text()
    assert "publicNetworkAccess: 'Disabled'" in text
    assert "firewallRules" not in text
    assert "'postgresqlServer'" in text, "the server is reached through a private endpoint"
    network = (INFRA / "network.bicep").read_text()
    assert "'privatelink.postgres.database.azure.com'" in network
    assert "serviceName: 'Microsoft.App/environments'" in network
    assert "infrastructureSubnetId: infrastructureSubnetId" in (INFRA / "containerapp.bicep").read_text()


def test_the_vault_audit_log_goes_to_log_analytics():
    text = (INFRA / "keyvault.bicep").read_text()
    assert "Microsoft.Insights/diagnosticSettings" in text and "categoryGroup: 'audit'" in text


def test_the_single_replica_is_always_on():
    text = (INFRA / "containerapp.bicep").read_text()
    assert "minReplicas: 1" in text and "maxReplicas: 1" in text


def test_the_blob_container_is_private_and_versioned():
    text = (INFRA / "storage.bicep").read_text()
    assert "allowBlobPublicAccess: false" in text
    assert "publicAccess: 'None'" in text
    assert "isVersioningEnabled: true" in text
    assert "minimumTlsVersion: 'TLS1_2'" in text


def test_previous_blob_versions_expire_after_the_day_parameter():
    # Security pack N1: with versioning on and no lifecycle rule, a file deleted in the app stays a
    # previous version for ever. The rule must delete previous versions of the app's blobs, after a
    # parameterised age, and never touch the current version of a live file.
    text = (INFRA / "storage.bicep").read_text()
    match = re.search(
        r"^resource (\w+) 'Microsoft\.Storage/storageAccounts/managementPolicies@2023-05-01' = \{\n(.*?)^\}",
        text, re.MULTILINE | re.DOTALL)
    assert match, "storage.bicep defines no lifecycle management policy"
    body = match.group(2)
    assert "parent: account" in body
    assert "name: 'default'" in body
    assert "type: 'Lifecycle'" in body
    assert re.search(r"blobTypes: \[\s*'blockBlob'\s*\]", body)
    assert re.search(r"prefixMatch: \[\s*'\$\{containerName\}/'\s*\]", body), "the rule must be limited to the evidence container"
    assert re.search(r"version: \{\s*delete: \{\s*daysAfterCreationGreaterThan: previousVersionDays\s*\}", body)
    assert "baseBlob" not in body, "the rule must never act on the current version of a live file"
    assert "snapshot" not in body
    assert declared_params(text)["previousVersionDays"]
    main = (INFRA / "main.bicep").read_text()
    storage = dict((symbol, body) for symbol, _, body in module_blocks(main))["storage"]
    assert re.search(r"^    previousVersionDays: blobPreviousVersionDays$", storage, re.MULTILINE)


def test_the_generated_names_fit_inside_the_azure_limits():
    # uniqueString() is always 13 characters. A storage account name and a Key Vault name may be
    # no longer than 24, so the prefix has to be bounded.
    text = (INFRA / "main.bicep").read_text()
    longest_prefix = int(re.search(r"@maxLength\((\d+)\)\nparam namePrefix ", text).group(1))
    for variable, limit in (("storageAccountName", 24), ("keyVaultName", 24), ("registryName", 50)):
        pattern = re.search(rf"^var {variable} = toLower\('(.*)'\)$", text, re.MULTILINE).group(1)
        literal = len(re.sub(r"\$\{\w+\}", "", pattern))
        assert literal + longest_prefix + 13 <= limit, f"{variable} can exceed {limit} characters"


def test_postgres_is_version_16():
    text = (INFRA / "postgres.bicep").read_text()
    assert "param postgresVersion string = '16'" in text


def test_the_deploy_workflow_only_runs_when_a_person_asks():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert set(triggers) == {"workflow_dispatch"}
    assert workflow["permissions"]["id-token"] == "write"
    assert workflow["permissions"]["contents"] == "read"
    job = workflow["jobs"]["deploy"]
    assert job["concurrency"]["group"] if "concurrency" in job else workflow["concurrency"]["group"]
    body = WORKFLOW.read_text()
    assert "azure/login@v2" in body, "sign-in goes through the OIDC login action"
    assert "client-secret" not in body and "creds:" not in body, "no secret value in the workflow"


def test_the_workflow_sends_no_traffic_unless_the_dispatch_asks():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert triggers["workflow_dispatch"]["inputs"]["send_traffic"]["default"] is False
    steps = workflow["jobs"]["deploy"]["steps"]
    traffic = [step for step in steps if "traffic set" in step.get("run", "")]
    promote = [step for step in traffic if "inputs.send_traffic" in str(step.get("if", ""))]
    assert len(promote) == 1, "exactly one step moves traffic, and only on request"


def test_the_outputs_the_runbook_reads_are_the_ones_the_template_produces():
    outputs = declared_outputs((INFRA / "main.bicep").read_text())
    quoted = set(re.findall(r'json\.load\(sys\.stdin\)\["(\w+)"\]', DOCS.read_text()))
    assert quoted, "the runbook should show how to read the deployment outputs"
    assert quoted <= outputs, f"docs/AZURE.md reads outputs that do not exist: {sorted(quoted - outputs)}"
    assert "appUrl" in outputs, "the template has to output the address of the app"


def test_the_documented_settings_are_the_ones_the_workflow_uses():
    body = WORKFLOW.read_text()
    documentation = DOCS.read_text()
    names = set(re.findall(r"\b(?:secrets|vars)\.(\w+)", body))
    assert names, "the workflow should read its configuration from secrets and variables"
    missing = {name for name in names if name not in documentation}
    assert not missing, f"docs/AZURE.md does not say how to set {sorted(missing)}"


def test_the_app_verifies_the_database_certificate():
    # `sslmode=require` encrypts but accepts any certificate for any name (security review M1).
    # `sslrootcert=system` is no substitute here: psycopg-binary's bundled OpenSSL looks for the system
    # roots in its own build directory, so the Debian bundle has to be named.
    main = (INFRA / "main.bicep").read_text()
    tls = re.search(r"^var databaseTls = '([^']*)'$", main, re.MULTILINE)
    assert tls, "the database URL's TLS settings live in one variable"
    assert tls.group(1) == "sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt"
    url = re.search(r"^var databaseUrl = '(.*)'$", main, re.MULTILINE).group(1)
    assert url.endswith("?${databaseTls}") and "sslmode=" not in url
    # The certificate names the server's usual host, which the private zone points at the endpoint.
    assert "@${postgres.outputs.fullyQualifiedDomainName}:5432/" in url
    assert "privatelink" not in url
    dockerfile = (ROOT / "Dockerfile").read_text()
    # The base may carry its digest (security review L11); the tag still names the Debian release.
    assert re.search(r"^FROM python:[\d.]+-slim-bookworm(@sha256:[0-9a-f]{64})?$", dockerfile, re.MULTILINE), \
        "the CA path is Debian's"
    assert re.search(r"^\s+ca-certificates \\$", dockerfile, re.MULTILINE)


def test_the_app_role_switch_ships_in_the_image_and_the_runbook():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY scripts/azure_db_app_role.py ./scripts/azure_db_app_role.py" in dockerfile
    documentation = DOCS.read_text()
    for step in ("python scripts/azure_db_app_role.py create", "python scripts/azure_db_app_role.py check",
                 "invstudio_app", "read -rs"):
        assert step in documentation, f"docs/AZURE.md does not show {step}"


def test_key_vault_purge_protection_is_on_unless_turned_off():
    main = (INFRA / "main.bicep").read_text()
    assert "param enableKeyVaultPurgeProtection bool = true" in main
    assert "param keyVaultSoftDeleteRetentionDays int = 90" in main
    vault = (INFRA / "keyvault.bicep").read_text()
    assert "param enablePurgeProtection bool = true" in vault
    assert "softDeleteRetentionInDays: softDeleteRetentionInDays" in vault
    # The API refuses `false` once purge protection is on, so the template leaves it unset instead.
    assert "enablePurgeProtection: enablePurgeProtection ? true : null" in vault


def test_blob_and_registry_access_is_audited():
    # Their endpoints stay public behind Entra ID (security review L4), so every use is logged.
    storage = (INFRA / "storage.bicep").read_text()
    assert "scope: blobService" in storage
    for category in ("StorageRead", "StorageWrite", "StorageDelete"):
        assert f"category: '{category}'" in storage
    registry = (INFRA / "registry.bicep").read_text()
    assert "scope: registry" in registry
    for category in ("ContainerRegistryLoginEvents", "ContainerRegistryRepositoryEvents"):
        assert f"category: '{category}'" in registry


def test_the_spoke_range_comes_from_ipam_and_is_never_guessed():
    # Access request H1: the /24 is the platform's IPAM answer. A default would be a guessed range that can
    # overlap the hub, so the parameter is required and the example file carries the placeholder.
    main = (INFRA / "main.bicep").read_text()
    assert declared_params(main).get("networkAddressPrefix") is False
    assert "<SPOKE_VNET_CIDR>" in main
    assert '"networkAddressPrefix": { "value": "<SPOKE_VNET_CIDR>" }' in (INFRA / "main.parameters.example.json").read_text()
    network = (INFRA / "network.bicep").read_text()
    assert declared_params(network).get("addressPrefix") is False
    assert not re.search(r"\b10\.\d+\.\d+\.\d+/\d+", main + network), "no address range is written into the template"


def test_hub_peering_and_dns_are_optional_and_passed_through():
    main = (INFRA / "main.bicep").read_text()
    params = declared_params(main)
    assert params.get("hubVnetId") is True and params.get("dnsServers") is True
    assert re.search(r"^param hubVnetId string = ''$", main, re.MULTILINE)
    assert re.search(r"^param dnsServers array = \[\]$", main, re.MULTILINE)
    network = (INFRA / "network.bicep").read_text()
    assert "virtualNetworkPeerings@" in network and "= if (!empty(hubVnetId))" in network
    assert "useRemoteGateways: false" in network and "allowForwardedTraffic: true" in network
    assert re.search(r"dhcpOptions: empty\(dnsServers\) \? null : \{\s+dnsServers: dnsServers", network)
    for name in ("vnetName", "vnetId", "appsSubnetId", "endpointsSubnetId", "hubPeeringName"):
        assert name in declared_outputs(main), f"main.bicep should output {name}"


def test_both_subnets_have_an_nsg_that_denies_other_inbound():
    network = (INFRA / "network.bicep").read_text()
    for subnet in ("apps", "endpoints"):
        block = network.split(f"name: '{subnet}'", 1)[1].split("\n      }\n", 1)[0]
        assert f"id: {subnet}Nsg.id" in block, f"the {subnet} subnet has no NSG"
        nsg = network.split(f"resource {subnet}Nsg ", 1)[1].split("\n}\n", 1)[0]
        assert re.search(r"name: 'deny-all-other-inbound'\s+properties: \{\s+priority: 4096\s+direction: 'Inbound'\s+"
                         r"access: 'Deny'", nsg), f"the {subnet} NSG must end in a deny-all inbound rule"
    # A workload-profiles environment needs a delegated /27 or larger; private endpoints obey the NSG.
    assert "var appsPrefix = cidrSubnet(addressPrefix, 27, 0)" in network
    assert "privateEndpointNetworkPolicies: 'NetworkSecurityGroupEnabled'" in network
    assert "'AzureLoadBalancer'" in network and "'30000-32767'" in network
    assert "destinationPortRange: '5432'" in network


def test_the_environment_turns_internal_on_one_flag():
    main = (INFRA / "main.bicep").read_text()
    assert re.search(r"^param internalEnvironment bool = false$", main, re.MULTILINE), \
        "false keeps the public first deploy; the target redeploy sets true"
    app = (INFRA / "containerapp.bicep").read_text()
    assert "internal: internalEnvironment" in app
    # In an internal environment, external ingress means reachable from the virtual network only. false
    # would hide the app from the hub entry point and change its host name.
    assert re.search(r"ingress: \{(?:\s+//[^\n]*)*\s+external: true", app)
    assert "clientSource = internalEnvironment ? 'VirtualNetwork' : 'Internet'" in (INFRA / "network.bicep").read_text()
    for name in ("appEnvironmentStaticIp", "appEnvironmentDefaultDomain"):
        assert name in declared_outputs(main)
