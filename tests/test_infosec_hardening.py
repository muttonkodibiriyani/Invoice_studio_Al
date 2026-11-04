"""The InfoSec LLD §9 open points, closed in code and templates.

- outbound traffic limited to named Azure services (network.bicep), optionally through the hub firewall
- Key Vault and Blob behind private endpoints, public access switchable off (privatelink.bicep, azure_lockdown.py)
- Azure AI Foundry with the app's managed identity instead of an api-key (azure_foundry.py)
- only the AI providers the deployment names (INV_STUDIO_AI_PROVIDERS), so an admin cannot add a non-Azure one
- restart, 5xx and database alerts (alerts.bicep); optional client IP ranges at the ingress
"""
import re
from pathlib import Path

import pytest

from app import azure_foundry
from app.providers import Providers, allowed_ai_providers
from app.store import Store
from tests.conftest import bearer
from tests.test_batch import OPTIONS, FakeReader, studio

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra" / "azure"


# Templates

def test_outbound_is_limited_to_named_azure_services_and_the_internet_is_denied():
    text = (INFRA / "network.bicep").read_text()
    assert "param restrictEgress bool = true" in text
    for tag in ("AzureActiveDirectory", "MicrosoftContainerRegistry", "AzureFrontDoor.FirstParty",
                "AzureContainerRegistry", "'Storage'", "AzureKeyVault", "AzureMonitor"):
        assert tag in text, tag
    assert re.search(r"name: 'deny-out-internet'.*?direction: 'Outbound'.*?access: 'Deny'.*?destinationAddressPrefix: 'Internet'",
                     text, re.S)
    assert "securityRules: concat([" in text and "restrictEgress ? egressRules : []" in text
    # Azure DNS must never be denied, or the environment stops working (Microsoft's NSG page).
    assert "168.63.129.16" not in re.sub(r"//.*", "", text)


def test_the_hub_firewall_route_is_optional():
    text = (INFRA / "network.bicep").read_text()
    assert "resource egressRoutes 'Microsoft.Network/routeTables@2024-05-01' = if (!empty(egressFirewallIp))" in text
    assert "nextHopType: 'VirtualAppliance'" in text and "addressPrefix: '0.0.0.0/0'" in text
    assert "routeTable: empty(egressFirewallIp) ? null" in text


def test_vault_and_blob_get_private_endpoints_and_can_close_their_public_ones():
    link = (INFRA / "privatelink.bicep").read_text()
    assert "groupIds: [\n            'vault'" in link and "groupIds: [\n            'blob'" in link
    assert "privatelink.vaultcore.azure.net" in link and "privatelink.blob.${environment().suffixes.storage}" in link
    main = (INFRA / "main.bicep").read_text()
    assert "module privatelink 'privatelink.bicep'" in main
    for module in ("storage.bicep", "keyvault.bicep"):
        text = (INFRA / module).read_text()
        assert "publicNetworkAccess: publicNetworkAccess" in text, module
        assert "defaultAction: publicNetworkAccess == 'Enabled' ? 'Allow' : 'Deny'" in text, module
    assert main.count("publicNetworkAccess: dataPlanePublicAccess") == 2


def test_alerts_cover_restarts_server_errors_and_the_database():
    text = (INFRA / "alerts.bicep").read_text()
    for metric in ("'RestartCount'", "'Requests'", "'5xx'", "'cpu_percent'", "'storage_percent'"):
        assert metric in text, metric
    assert "module alerts 'alerts.bicep'" in (INFRA / "main.bicep").read_text()


def test_the_container_app_limits_ai_providers_and_can_limit_clients():
    text = (INFRA / "containerapp.bicep").read_text()
    assert "param aiProviders string = 'azure'" in text
    assert "ipSecurityRestrictions: [for (cidr, i) in ingressAllowedCidrs" in text
    assert "name: 'INV_STUDIO_AZURE_AUTH'" in text and "value: foundryAuthMode" in text


# Azure AI Foundry with the managed identity

def test_managed_identity_needs_no_key_and_sends_a_bearer_token(tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_AZURE_ENDPOINT", "https://my-resource.openai.azure.com")
    monkeypatch.delenv("INV_STUDIO_AZURE_API_KEY", raising=False)
    monkeypatch.setenv("INV_STUDIO_AZURE_AUTH", "managed_identity")
    scopes = []
    monkeypatch.setattr(azure_foundry, "bearer_token", lambda scope=None: scopes.append(scope) or "entra-token")
    foundry = azure_foundry.AzureFoundry(Store(tmp_path))
    assert foundry.configured()
    assert foundry.credentials() == ("https://my-resource.openai.azure.com/openai/v1", None)
    assert foundry.auth_headers() == {"Authorization": "Bearer entra-token"}
    providers = Providers(Store(tmp_path), chatgpt=None)
    assert providers.headers("azure") == {"Authorization": "Bearer entra-token"}


def test_managed_identity_ignores_a_mounted_key(tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_AZURE_ENDPOINT", "https://my-resource.openai.azure.com")
    monkeypatch.setenv("INV_STUDIO_AZURE_API_KEY", "set-me-with-az-keyvault-secret-set")
    monkeypatch.setenv("INV_STUDIO_AZURE_AUTH", "managed_identity")
    monkeypatch.setattr(azure_foundry, "bearer_token", lambda scope=None: "t")
    assert azure_foundry.AzureFoundry(Store(tmp_path)).auth_headers() == {"Authorization": "Bearer t"}


def test_key_mode_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_AZURE_ENDPOINT", "https://my-resource.openai.azure.com")
    monkeypatch.setenv("INV_STUDIO_AZURE_API_KEY", "k-123")
    monkeypatch.delenv("INV_STUDIO_AZURE_AUTH", raising=False)
    assert azure_foundry.AzureFoundry(Store(tmp_path)).auth_headers() == {"api-key": "k-123"}


def test_a_failed_token_is_a_plain_error(tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_AZURE_ENDPOINT", "https://my-resource.openai.azure.com")
    monkeypatch.setenv("INV_STUDIO_AZURE_AUTH", "managed_identity")

    def broken(scope=None):
        raise RuntimeError("ManagedIdentityCredential: IMDS at 169.254.169.254 unreachable")

    monkeypatch.setattr(azure_foundry, "bearer_token", broken)
    with pytest.raises(ValueError, match="Cognitive Services OpenAI User") as err:
        azure_foundry.AzureFoundry(Store(tmp_path)).auth_headers()
    assert "169.254" not in str(err.value)


@pytest.mark.parametrize("value, expected", [
    ("", azure_foundry.DEFAULT_SCOPE),
    ("api://citadel-gateway/.default", "api://citadel-gateway/.default"),
    ("https://evil.example/x", azure_foundry.DEFAULT_SCOPE),
])
def test_the_token_scope_is_a_default_scope_or_nothing(monkeypatch, value, expected):
    monkeypatch.setenv("INV_STUDIO_AZURE_TOKEN_SCOPE", value)
    assert azure_foundry.token_scope() == expected


# Only the AI providers the deployment names

def test_unset_means_every_provider(monkeypatch):
    monkeypatch.delenv("INV_STUDIO_AI_PROVIDERS", raising=False)
    assert "openai" in allowed_ai_providers() and "azure" in allowed_ai_providers()


def test_azure_only_refuses_other_providers_everywhere(tmp_path, monkeypatch):
    app, client, admin = studio(tmp_path, monkeypatch, FakeReader(), INV_STUDIO_AI_PROVIDERS="azure")
    h = bearer(admin)
    assert client.post("/api/connections/openai", json={"api_key": "sk-test-123456"}, headers=h).status_code == 403
    assert client.get("/api/connections/anthropic/models", headers=h).status_code == 403
    assert client.post("/api/subscriptions/claude/import", json={"setup_token": "x" * 40}, headers=h).status_code in (403, 422)
    ai = {**OPTIONS, "ai_fallback": True, "provider": "openai"}
    r = client.post("/api/preflight", json={"options": ai, "files": [{"name": "a.txt", "size": 1}]}, headers=h)
    assert r.status_code == 403 and "only these AI providers: azure" in r.json()["detail"]
    # Azure itself, and local reading, still go through.
    ok = {**OPTIONS, "ai_fallback": True, "provider": "azure"}
    assert client.post("/api/preflight", json={"options": ok, "files": [{"name": "a.txt", "size": 1}]}, headers=h).status_code == 200
    assert client.post("/api/preflight", json={"options": OPTIONS, "files": [{"name": "a.txt", "size": 1}]}, headers=h).status_code == 200


def test_the_default_provider_follows_the_limit(tmp_path, monkeypatch):
    app, client, admin = studio(tmp_path, monkeypatch, FakeReader(), INV_STUDIO_AI_PROVIDERS="azure")
    state = client.get("/api/state", headers=bearer(admin)).json()
    assert state["settings"]["provider"] == "azure"


def test_the_engine_never_calls_a_provider_outside_the_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_AI_PROVIDERS", "azure")
    with pytest.raises(PermissionError):
        Providers(Store(tmp_path), chatgpt=None).headers("openai")


# Signed-in readiness

def test_dependencies_needs_a_sign_in(client):
    assert client.get("/api/health/dependencies").status_code == 401


def test_dependencies_reports_ok_and_failed_without_details(tmp_path, monkeypatch):
    app, client, admin = studio(tmp_path, monkeypatch, FakeReader())
    r = client.get("/api/health/dependencies", headers=bearer(admin))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    app.state.store.check_dependencies = lambda: {"database": "ok", "storage": "failed: ServiceRequestError"}
    r = client.get("/api/health/dependencies", headers=bearer(admin))
    assert r.status_code == 503 and r.json() == {"status": "degraded", "database": "ok", "storage": "failed: ServiceRequestError"}
