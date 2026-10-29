"""scripts/azure_deploy.py against a stubbed az: no Azure, no sign-in, nothing created.

The stub answers each az command the way Azure does for a subscription that is ready. Each refusal test
breaks one answer and checks that the script stops with that one reason and never reaches
`az deployment group create`.
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT_EXAMPLE = ROOT / "infra" / "azure" / "deploy-kit.example.env"
SUB = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
USER_ID = "33333333-3333-3333-3333-333333333333"
RG = "rg-test"
SCOPE = f"/subscriptions/{SUB}/resourceGroups/{RG}"
FERNET = "A" * 43 + "="


def load_script():
    spec = importlib.util.spec_from_file_location("azure_deploy", ROOT / "scripts" / "azure_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_script()


def bicep_parameters():
    """main.bicep's parameters as `az bicep build` reports them: a default makes one optional."""
    params = {}
    for match in re.finditer(r"^param (\w+) \w+( =)?", (ROOT / "infra" / "azure" / "main.bicep").read_text(), re.M):
        params[match.group(1)] = {"type": "x", **({"defaultValue": "x"} if match.group(2) else {})}
    return params


def filled_kit(tmp_path, **overrides):
    values = {key: f"value-{key.lower()}" for key in deploy.KIT_KEYS}
    values.update(AZ_SUBSCRIPTION_ID=SUB, AZ_TENANT_ID=TENANT, AZ_RESOURCE_GROUP=RG, AZ_REGION="uaenorth",
                  AZ_REQUIRED_TAGS='{"costCentre": "cc-1"}', AZ_NAME_PREFIX="invstudio",
                  TARKESH_UPN="tarkesh@example.com", SPOKE_VNET_CIDR="10.1.2.0/24", HUB_DNS_IP="10.0.0.4",
                  PDNS_ZONE_IDS="{}", LOG_RETENTION_DAYS="30", APP_HOSTNAME="invoices.example.com",
                  LAW_ID="n/a", ENTRA_GROUP_MEMBER_NAME="n/a", ENTRA_CLOUD="public")
    values.update(overrides)
    path = tmp_path / "deploy-kit.env"
    path.write_text("".join(f"{k}={v}   # comment\n" for k, v in values.items() if v is not None))
    return path


class FakeAz:
    """Answers az commands like a ready subscription. Tests override one answer to break it."""

    def __init__(self):
        params = bicep_parameters()
        self.calls = []
        self.params_seen = None
        self.answers = {
            "bicep build": {"parameters": params},
            "account show": {"id": SUB, "tenantId": TENANT},
            "ad signed-in-user show": {"id": USER_ID, "userPrincipalName": "Tarkesh@example.com"},
            "group show": {"id": SCOPE, "location": "uaenorth"},
            "resource list": [],
            "role assignment list": [
                {"roleDefinitionId": f"/providers/x/roleDefinitions/{deploy.CONTRIBUTOR}", "condition": None},
                {"roleDefinitionId": f"/providers/x/roleDefinitions/{deploy.RBAC_ADMIN}",
                 "condition": " ".join(f"@Request[...] ForAnyOfAnyValues:GuidEquals {{{g}}}"
                                       for g in deploy.TEMPLATE_ROLES)},
            ],
            "provider show": "Registered",
            "postgres flexible-server list-skus": [
                {"supportedServerEditions": [{"name": "Burstable", "supportedServerSkus": [{"name": "Standard_B2s"}]}]}],
            "rest": {"value": [{"name": {"value": "ManagedEnvironmentCount"}, "currentValue": 0, "limit": 15}]},
            "deployment group validate": {},
            "deployment group what-if": {"status": "Succeeded",
                                         "changes": [{"changeType": "Create"}, {"changeType": "Create"}]},
            "deployment group create": {},
            "deployment group show": {"appUrl": {"value": "https://app.example"}, "appName": {"value": "a"},
                                      "acrName": {"value": "r"}, "vaultName": {"value": "v"},
                                      "blobAccountName": {"value": "s"}, "databaseHost": {"value": "d"},
                                      "databaseServerName": {"value": "db"}},
        }

    def __call__(self, *args):
        self.calls.append(args)
        for command, answer in self.answers.items():
            if " ".join(args).startswith(command):
                if "-p" in args and self.params_seen is None:
                    self.params_seen = json.loads(Path(args[args.index("-p") + 1][1:]).read_text())["parameters"]
                if isinstance(answer, Exception):
                    raise answer
                return answer(args) if callable(answer) else answer
        raise AssertionError(f"unexpected az call: {args}")

    def created(self):
        return any(call[:3] == ("deployment", "group", "create") for call in self.calls)


def run(tmp_path, az, *flags, kit=None, confirm="DEPLOY"):
    return deploy.main(["--kit", str(kit or filled_kit(tmp_path)), *flags], run=az,
                       ask=lambda: ("a-long-db-password", FERNET), confirm=lambda prompt: confirm)


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "STATE", tmp_path / "state")


def stop_reason(capsys):
    err = capsys.readouterr().err
    assert "STOP:" in err
    return err


# The kit and the access request

def test_example_kit_has_exactly_the_access_request_placeholders():
    lines = [line.split("#")[0].strip() for line in KIT_EXAMPLE.read_text().splitlines()]
    pairs = [line.split("=", 1) for line in lines if line]
    assert [key for key, _ in pairs] == list(deploy.KIT_KEYS)
    assert all(value == f"<{key}>" for key, value in pairs)


def test_access_request_placeholders_are_all_in_the_kit():
    # Every distinct <NAME> in the tm8 doc "Azure access request" (01a187ad-7fd4, v4), except the generic
    # <PLACEHOLDER> its introduction uses.
    access_request = """AZ_SUBSCRIPTION_ID AZ_TENANT_ID AZ_RESOURCE_GROUP AZ_REGION AZ_REQUIRED_TAGS AZ_NAME_PREFIX
        AZ_POLICY_EXEMPTIONS TARKESH_UPN ENTRA_APP_DISPLAY_NAME ENTRA_APP_CLIENT_ID ENTRA_REDIRECT_URI_OWNER
        ENTRA_GROUP_ADMIN ENTRA_GROUP_ADMIN_NAME ENTRA_GROUP_MEMBER ENTRA_GROUP_MEMBER_NAME ENTRA_GROUP_OWNERS
        ENTRA_CA_NOTES ENTRA_TOKEN_LIFETIME ENTRA_CLOUD ENTRA_SIGNIN_LOGS
        DEPLOY_CLIENT_ID GH_ENV_REVIEWERS ACR_TASKS_NETWORK_PATH CLOUDSHELL_STORAGE CLOUDSHELL_VNET SPOKE_VNET_CIDR
        HUB_VNET_ID HUB_DNS_IP HUB_FIREWALL_IP FW_RULE_COLLECTION PDNS_ZONE_IDS AMPLS_ID REGISTRY_CHOICE
        SHARED_ACR_LOGIN_SERVER CITADEL_ENDPOINT CITADEL_DEPLOYMENT CITADEL_AUTH_MODE CITADEL_APPROVAL_REF
        APP_HOSTNAME INGRESS_ENTRY TLS_CERT_REF LAW_ID LOG_RETENTION_DAYS""".split()
    assert sorted(access_request) == sorted(deploy.KIT_KEYS)


@pytest.mark.parametrize("cloud", ["public", "AzureCloud", "https://login.microsoftonline.com/"])
def test_public_cloud_spellings_pass(tmp_path, cloud):
    assert run(tmp_path, FakeAz(), "--check", kit=filled_kit(tmp_path, ENTRA_CLOUD=cloud)) == 0


def test_every_kit_mapping_names_a_kit_line():
    assert set(deploy.PARAMS) <= set(deploy.KIT_KEYS)


def test_mapped_parameters_that_exist_today_are_in_main_bicep():
    declared = bicep_parameters()
    for name in ("location", "namePrefix", "tags", "networkAddressPrefix", "foundryEndpoint",
                 "foundryDeployment", "logRetentionInDays", "extraAllowedOrigins"):
        assert name in declared


def test_refuses_the_untouched_example_kit(tmp_path, capsys):
    az = FakeAz()
    assert run(tmp_path, az, kit=KIT_EXAMPLE) == 1
    assert "AZ_SUBSCRIPTION_ID is not filled in" in stop_reason(capsys)
    assert az.calls == []


@pytest.mark.parametrize("override, reason", [
    ({"AMPLS_ID": "<AMPLS_ID>"}, "AMPLS_ID is not filled in"),
    ({"CLOUDSHELL_VNET": ""}, "CLOUDSHELL_VNET is not filled in"),
    ({"TLS_CERT_REF": None}, "missing TLS_CERT_REF"),
    ({"SOMETHING_ELSE": "x"}, "line the access request does not: SOMETHING_ELSE"),
    ({"ENTRA_CLOUD": "AzureUSGovernment"}, "not the public cloud"),
])
def test_refuses_an_unfilled_or_wrong_kit_before_calling_az(tmp_path, capsys, override, reason):
    az = FakeAz()
    assert run(tmp_path, az, kit=filled_kit(tmp_path, **override)) == 1
    assert reason in stop_reason(capsys)
    assert az.calls == []


# Each preflight check stops with its own reason, before anything is created

def _no_access(*args):
    raise deploy.AzError("ERROR: not found")


@pytest.mark.parametrize("command, answer, reason", [
    ("bicep build", deploy.AzError("Error BCP018"), "Bicep cannot build main.bicep"),
    ("account show", deploy.AzError("Please run 'az login'"), "not signed in"),
    ("account show", {"id": SUB, "tenantId": "other"}, "not AZ_TENANT_ID"),
    ("account show", {"id": "other", "tenantId": TENANT}, "az account set --subscription"),
    ("ad signed-in-user show", {"id": USER_ID, "userPrincipalName": "someone@example.com"}, "not TARKESH_UPN"),
    ("group show", deploy.AzError("ResourceGroupNotFound"), f"resource group {RG} does not exist"),
    ("group show", {"id": SCOPE, "location": "westeurope"}, "not AZ_REGION"),
    ("resource list", [{"name": "leftover"}], "is not empty"),
    ("role assignment list", [], "do not have Contributor"),
    ("role assignment list",
     [{"roleDefinitionId": f"/x/{deploy.CONTRIBUTOR}", "condition": None}], "cannot create role assignments"),
    ("role assignment list",
     [{"roleDefinitionId": f"/x/{deploy.CONTRIBUTOR}", "condition": None},
      {"roleDefinitionId": f"/x/{deploy.RBAC_ADMIN}", "condition": "GuidEquals {7f951dda-4ed3-4680-a7ca-43fe172d538d}"}],
     "does not allow Storage Blob Data Contributor"),
    ("provider show", "NotRegistered", "Microsoft.App is NotRegistered"),
    ("postgres flexible-server list-skus", [{"supportedServerEditions": []}], "Standard_B2s is not offered"),
    ("rest", {"value": [{"name": {"value": "ManagedEnvironmentCount"}, "currentValue": 15, "limit": 15}]},
     "quota 'ManagedEnvironmentCount' is full"),
    ("deployment group validate",
     deploy.AzError("ERROR: RequestDisallowedByPolicy: Resource 'kv' was disallowed by policy 'deny-public'"),
     "Azure Policy refuses the deployment"),
    ("deployment group what-if", deploy.AzError("InvalidTemplate"), "ARM refuses the deployment"),
    ("deployment group what-if", {"status": "Failed", "error": {"code": "X"}}, "what-if failed"),
])
def test_each_failing_check_stops_before_the_deploy(tmp_path, capsys, command, answer, reason):
    az = FakeAz()
    az.answers[command] = answer
    assert run(tmp_path, az) == 1
    err = stop_reason(capsys)
    assert reason in err
    assert err.count("STOP:") == 1
    assert not az.created()


def test_pim_hint_when_roles_are_missing(tmp_path, capsys):
    az = FakeAz()
    az.answers["role assignment list"] = []
    run(tmp_path, az)
    assert "activate the eligible roles" in stop_reason(capsys)


def test_owner_is_enough(tmp_path):
    az = FakeAz()
    az.answers["role assignment list"] = [{"roleDefinitionId": f"/x/{deploy.OWNER}", "condition": None}]
    assert run(tmp_path, az) == 0


def test_rerun_allows_a_group_that_is_not_empty(tmp_path):
    az = FakeAz()
    az.answers["resource list"] = [{"name": "leftover"}]
    assert run(tmp_path, az, "--rerun") == 0
    assert not any(call[:2] == ("resource", "list") for call in az.calls)


def test_the_kit_gives_every_parameter_main_bicep_requires(tmp_path, capsys):
    az = FakeAz()  # main.bicep as it is: the Entra settings, no Firebase
    assert run(tmp_path, az, "--check") == 0
    assert "template takes the kit" in capsys.readouterr().out


def test_stops_when_main_bicep_requires_a_parameter_the_kit_does_not_give(tmp_path, capsys):
    az = FakeAz()
    az.answers["bicep build"]["parameters"]["newRequiredSetting"] = {"type": "string"}
    assert run(tmp_path, az) == 1
    assert "main.bicep requires newRequiredSetting" in stop_reason(capsys)
    assert not az.created()


def test_bad_kit_values_are_refused(tmp_path, capsys):
    assert run(tmp_path, FakeAz(), kit=filled_kit(tmp_path, AZ_REQUIRED_TAGS="cost=1")) == 1
    assert "AZ_REQUIRED_TAGS is not in the form" in stop_reason(capsys)
    assert run(tmp_path, FakeAz(), kit=filled_kit(tmp_path, AZ_NAME_PREFIX="Inv-Studio")) == 1
    assert "AZ_NAME_PREFIX must be" in stop_reason(capsys)


# The happy path

def test_check_runs_the_preflight_and_creates_nothing(tmp_path, capsys):
    az = FakeAz()
    assert deploy.main(["--kit", str(filled_kit(tmp_path)), "--check"], run=az,
                       ask=lambda: pytest.fail("--check must not ask for secrets"),
                       confirm=lambda prompt: pytest.fail("--check must not ask to deploy")) == 0
    out = capsys.readouterr().out
    assert "Preflight passed. Nothing was created." in out
    assert "not counted as passed" in out and "ACR Tasks" in out
    assert not az.created()


def test_deploy_passes_the_kit_and_cleans_up(tmp_path, capsys):
    az = FakeAz()
    assert run(tmp_path, az) == 0
    assert az.created()
    p = {k: v["value"] for k, v in az.params_seen.items()}
    assert p["location"] == "uaenorth" and p["namePrefix"] == "invstudio"
    assert p["tags"] == {"application": "inv-studio", "managedBy": "infra/azure/main.bicep", "costCentre": "cc-1"}
    assert p["networkAddressPrefix"] == "10.1.2.0/24" and p["logRetentionInDays"] == 30
    assert p["extraAllowedOrigins"] == ["https://invoices.example.com"]
    assert p["entraTenantId"] == TENANT and p["entraClientId"] == "value-entra_app_client_id"
    assert p["entraAdminGroupName"] == "value-entra_group_admin_name"
    assert "entraMemberGroupName" not in p  # n/a leaves the optional name at its empty default
    assert not any(name.startswith(("entraAdminRole", "entraMemberRole")) or name.endswith("GroupId") for name in p)
    assert p["postgresAdminPassword"] == "a-long-db-password" and p["vaultKey"] == FERNET
    assert "existingLogWorkspaceId" not in p  # n/a is not passed
    assert "hubVnetId" not in p  # main.bicep has no such parameter yet
    # The parameter file with the secrets is gone, and the secrets were never printed.
    create = next(c for c in az.calls if c[:3] == ("deployment", "group", "create"))
    assert not Path(create[create.index("-p") + 1][1:]).exists()
    out = capsys.readouterr()
    assert "a-long-db-password" not in out.out + out.err and FERNET not in out.out + out.err
    assert "not passed yet, main.bicep has no such parameter: HUB_VNET_ID -> hubVnetId" in out.out
    env = (deploy.STATE / "outputs.env").read_text()
    assert f"export RG={RG}" in env and "export APP_URL=https://app.example" in env
    assert "add https://app.example/ as an SPA redirect URI on the Entra app value-entra_app_client_id" in out.out


def test_deploy_needs_the_word_deploy(tmp_path, capsys):
    az = FakeAz()
    assert run(tmp_path, az, confirm="yes") == 1
    assert "not confirmed" in stop_reason(capsys)
    assert not az.created()


def test_a_failed_deployment_says_how_to_read_it_and_rerun(tmp_path, capsys):
    az = FakeAz()
    az.answers["deployment group create"] = deploy.AzError("DeploymentFailed")
    assert run(tmp_path, az) == 2
    err = capsys.readouterr().err
    assert "az deployment operation group list" in err and "--rerun" in err


def test_secrets_are_checked_before_use(monkeypatch):
    answers = iter(["short", FERNET])
    monkeypatch.setattr(deploy.getpass, "getpass", lambda prompt: next(answers))
    with pytest.raises(deploy.Stop, match="at least 12"):
        deploy.ask_secrets()
    answers = iter(["a-long-db-password", "not-a-key"])
    monkeypatch.setattr(deploy.getpass, "getpass", lambda prompt: next(answers))
    with pytest.raises(deploy.Stop, match="not a Fernet key"):
        deploy.ask_secrets()
