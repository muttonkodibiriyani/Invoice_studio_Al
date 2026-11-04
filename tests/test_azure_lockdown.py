"""scripts/azure_lockdown.py: closes Key Vault and Blob only behind working private endpoints, and never leaves the
app broken."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("azure_lockdown", ROOT / "scripts" / "azure_lockdown.py")
lockdown = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lockdown)

RG = "rg-inv"
VAULT_ID = "/subscriptions/s/resourceGroups/rg-inv/providers/Microsoft.KeyVault/vaults/kv"
STORAGE_ID = "/subscriptions/s/resourceGroups/rg-inv/providers/Microsoft.Storage/storageAccounts/st"


def endpoint(name, target, status="Approved"):
    return {"name": name, "privateLinkServiceConnections": [
        {"privateLinkServiceId": target, "privateLinkServiceConnectionState": {"status": status}}]}


class FakeAz:
    def __init__(self, health="Healthy"):
        self.calls = []
        self.access = "Enabled"
        self.health = health
        self.answers = {
            "keyvault list": ["kv"], "storage account list": ["st"], "containerapp list": ["app"],
            "keyvault show -n kv -g rg-inv --query id": VAULT_ID,
            "storage account show -n st -g rg-inv --query id": STORAGE_ID,
            "network private-endpoint list": [endpoint("kv-pe", VAULT_ID), endpoint("st-pe", STORAGE_ID)],
            "network private-endpoint dns-zone-group list": [{"name": "default"}],
            "keyvault update": self.update, "storage account update": self.update,
            "containerapp revision list": [{"name": "app--old", "traffic": 0}, {"name": "app--live", "traffic": 100}],
            "containerapp show": "app.example.azurecontainerapps.io",
            "containerapp revision restart": {},
            "containerapp revision show": lambda args: self.health if self.access == "Disabled" else "Healthy",
        }

    def update(self, args):
        self.access = args[args.index("--public-network-access") + 1]
        return {}

    def __call__(self, *args):
        self.calls.append(args)
        line = " ".join(args)
        for command, answer in self.answers.items():
            if line.startswith(command):
                return answer(args) if callable(answer) else answer
        raise AssertionError(f"unexpected az call: {args}")


@pytest.fixture(autouse=True)
def no_saved_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(lockdown, "STATE", tmp_path)
    monkeypatch.delenv("INV_LOCKDOWN_TOKEN", raising=False)


def run(az, *flags, token="", fetch=lambda url, token=None: 200):
    return lockdown.main(["-g", RG, *flags], run=az, read_token=lambda: token, fetch=fetch, sleep=lambda s: None)


def test_it_closes_both_and_proves_the_app_with_a_token():
    az = FakeAz()
    seen = []
    assert run(az, token="tok", fetch=lambda url, token=None: seen.append((url, token)) or 200) == 0
    assert az.access == "Disabled"
    assert ("containerapp", "revision", "restart", "-n", "app", "-g", RG, "--revision", "app--live") in az.calls
    assert ("https://app.example.azurecontainerapps.io/api/health/dependencies", "tok") in seen
    for call in az.calls:
        if call[1:2] == ("update",) or call[:3] == ("storage", "account", "update"):
            assert call[call.index("--default-action") + 1] == "Deny"


def test_an_unhealthy_app_after_the_lockdown_opens_everything_again(capsys):
    az = FakeAz(health="Unhealthy")
    assert run(az) == 2
    assert az.access == "Enabled"
    assert "Opening the public endpoints again" in capsys.readouterr().err


def test_a_failed_dependency_check_opens_everything_again():
    az = FakeAz()
    fetch = lambda url, token=None: 503 if url.endswith("/dependencies") else 200  # noqa: E731
    assert run(az, token="tok", fetch=fetch) == 2
    assert az.access == "Enabled"


@pytest.mark.parametrize("change, reason", [
    ({"network private-endpoint list": [endpoint("st-pe", STORAGE_ID)]}, "no private endpoint for Key Vault"),
    ({"network private-endpoint list": [endpoint("kv-pe", VAULT_ID, "Pending"), endpoint("st-pe", STORAGE_ID)]},
     "is Pending, not Approved"),
    ({"network private-endpoint dns-zone-group list": []}, "has no private DNS record"),
])
def test_nothing_is_closed_without_working_private_endpoints(capsys, change, reason):
    az = FakeAz()
    az.answers.update(change)
    assert run(az) == 1
    assert reason in capsys.readouterr().err
    assert not any(c[1:2] == ("update",) or c[:3] == ("storage", "account", "update") for c in az.calls)


def test_undo_and_status():
    az = FakeAz()
    az.access = "Disabled"
    assert run(az, "--undo") == 0 and az.access == "Enabled"
    az.answers["keyvault show -n kv -g rg-inv --query {"] = {"access": "Disabled", "default": "Deny"}
    az.answers["storage account show -n st -g rg-inv --query {"] = {"access": "Disabled", "default": "Deny"}
    assert run(az, "--status") == 0
