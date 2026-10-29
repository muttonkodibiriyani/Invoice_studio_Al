"""Azure AI Foundry: endpoint safety, the OpenAI v1 call on both endpoint styles, the spend ledger,
the managed default, and the admin-only connection API. Mocked HTTP only: no key, no network."""
import json
from pathlib import Path

import pytest

import app.azure_foundry as azure_module
import app.providers as provider_module
from app import costs
from app.azure_foundry import AzureFoundry, base_url, normalise_endpoint
from app.models import AI_PROVIDERS, ProcessingOptions
from app.providers import Providers
from app.store import Store
from tests.conftest import HEADERS, bearer
from tests.test_providers import FakeChatGPT, FakeClient, FakeResponse, sample_invoice


ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://acme.openai.azure.com"
FOUNDRY_ENDPOINT = "https://acme.services.ai.azure.com"
KEY = "azure-resource-key-0123456789"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture(autouse=True)
def _no_azure_environment(monkeypatch):
    for name in ("INV_STUDIO_AZURE_ENDPOINT", "INV_STUDIO_AZURE_API_KEY", "INV_STUDIO_AZURE_DEPLOYMENT",
                 "INV_STUDIO_DEFAULT_PROVIDER", "VERTEX_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)


def options(provider="azure", model="invoice-reader"):
    return ProcessingOptions(engine="ai", ai_fallback=True, provider=provider, model=model)


def completed(usage=None):
    return {"status": "completed",
            "output": [{"content": [{"type": "output_text", "text": json.dumps(sample_invoice())}]}],
            "usage": usage or {"input_tokens": 9, "output_tokens": 4}}


@pytest.mark.parametrize(
    "supplied,expected",
    [
        ("https://acme.openai.azure.com", "https://acme.openai.azure.com"),
        ("https://acme.openai.azure.com/", "https://acme.openai.azure.com"),
        ("acme.openai.azure.com", "https://acme.openai.azure.com"),
        ("  https://ACME.OpenAI.Azure.com  ", "https://acme.openai.azure.com"),
        ("https://acme.openai.azure.com/openai/v1/", "https://acme.openai.azure.com"),
        ("https://acme.services.ai.azure.com/openai/v1", "https://acme.services.ai.azure.com"),
        ("https://acme.cognitiveservices.azure.com/openai", "https://acme.cognitiveservices.azure.com"),
    ],
)
def test_an_azure_resource_endpoint_is_normalised_to_its_https_origin(supplied, expected):
    assert normalise_endpoint(supplied) == expected
    assert base_url(expected) == expected + "/openai/v1"


@pytest.mark.parametrize(
    "refused",
    [
        "http://acme.openai.azure.com",          # the server would send the key in clear
        "https://acme.openai.azure.com:8080",    # a redirected port on the resource host
        "https://user:secret@acme.openai.azure.com",
        "https://acme.openai.azure.com/admin",   # a path other than the data plane
        "https://acme.openai.azure.com?x=1",
        "https://openai.azure.com",              # the suffix itself is not a resource
        "https://evil.example.com",
        "https://acme.openai.azure.com.evil.example.com",
        "https://169.254.169.254",               # instance metadata
        "https://metadata.google.internal",      # the Cloud Run metadata server
        "https://localhost",
        "",
        "   ",
        None,
        "https://" + "a" * 400 + ".openai.azure.com",
    ],
)
def test_an_endpoint_the_server_must_not_fetch_is_refused(refused):
    with pytest.raises(ValueError):
        normalise_endpoint(refused)


def test_the_connection_is_fernet_encrypted_and_the_key_is_never_returned(store):
    connection = AzureFoundry(store)
    assert connection.configured() is False

    assert connection.connect(ENDPOINT, KEY) == {"connected": True, "provider": "azure", "endpoint": ENDPOINT}
    assert connection.configured() is True
    assert connection.endpoint() == ENDPOINT
    assert connection.credentials() == (ENDPOINT + "/openai/v1", KEY)
    # Saved through the same encrypted credentials table as the OpenAI and Anthropic keys.
    assert KEY.encode() not in store.path.read_bytes()
    with store.connection() as c:
        assert c.execute("SELECT count(*) FROM credentials WHERE key='azure'").fetchone()[0] == 1

    connection.disconnect()
    assert connection.configured() is False
    with pytest.raises(ValueError, match="Connect Azure AI Foundry"):
        connection.credentials()


def test_a_refused_endpoint_saves_nothing(store):
    connection = AzureFoundry(store)
    with pytest.raises(ValueError):
        connection.connect("https://evil.example.com", KEY)
    with pytest.raises(ValueError, match="API key"):
        connection.connect(ENDPOINT, "   ")
    assert connection.configured() is False


def test_deployment_configuration_connects_azure_without_the_ui(store, monkeypatch):
    """The hosted service is provisioned by environment variables, the key from Secret Manager."""
    connection = AzureFoundry(store)
    monkeypatch.setenv("INV_STUDIO_AZURE_ENDPOINT", "acme.services.ai.azure.com")
    monkeypatch.setenv("INV_STUDIO_AZURE_API_KEY", "env-supplied-key")
    assert connection.configured() is True
    assert connection.endpoint() == FOUNDRY_ENDPOINT
    assert connection.credentials() == (FOUNDRY_ENDPOINT + "/openai/v1", "env-supplied-key")
    assert connection.managed_default() == ""
    monkeypatch.setenv("INV_STUDIO_AZURE_DEPLOYMENT", "gpt-5-invoices")
    assert connection.managed_default() == "gpt-5-invoices"

    # A connection saved in the UI wins over the deployment default.
    connection.connect(ENDPOINT, KEY)
    assert connection.credentials() == (ENDPOINT + "/openai/v1", KEY)
    # Removing it falls back to the deployment default rather than leaving no reader.
    assert connection.disconnect() == {"connected": True, "provider": "azure"}
    assert connection.credentials() == (FOUNDRY_ENDPOINT + "/openai/v1", "env-supplied-key")


def test_a_half_configured_environment_does_not_connect(store, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_AZURE_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("INV_STUDIO_AZURE_DEPLOYMENT", "gpt-5-invoices")
    assert AzureFoundry(store).configured() is False
    assert AzureFoundry(store).managed_default() == ""
    assert azure_module.env_connection() is None


def test_a_deployment_name_the_model_field_would_refuse_is_not_a_default(monkeypatch):
    monkeypatch.setenv("INV_STUDIO_AZURE_DEPLOYMENT", "bad name;drop")
    assert azure_module.env_deployment() == ""


@pytest.mark.parametrize("endpoint", [ENDPOINT, FOUNDRY_ENDPOINT])
def test_azure_extract_calls_the_resource_responses_api_with_the_openai_body(tmp_path, store, monkeypatch, endpoint):
    """Both endpoint styles: <resource>.openai.azure.com and <resource>.services.ai.azure.com."""
    AzureFoundry(store).connect(endpoint, KEY)
    fake = FakeClient(post=FakeResponse(completed()))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: fake)
    path = tmp_path / "invoice.txt"
    path.write_text("visible invoice text")

    invoice, usage = Providers(store, FakeChatGPT()).extract(path, "visible invoice text", options())

    assert invoice.number == "DEMO-2026-001"
    assert usage == {"input_tokens": 9, "output_tokens": 4}
    _, url, request = fake.calls[0]
    assert url == endpoint + "/openai/v1/responses"
    # Azure authenticates with its own header, never a bearer token.
    assert request["headers"] == {"api-key": KEY}
    body = request["json"]
    # The model is the operator's deployment name, sent in the OpenAI Responses body.
    assert body["model"] == "invoice-reader"
    assert body["store"] is False
    assert body["max_output_tokens"] == 12000
    assert body["text"]["format"] == {"type": "json_schema", "name": "invoice", "strict": True,
                                      "schema": body["text"]["format"]["schema"]}
    assert body["text"]["format"]["schema"]["additionalProperties"] is False
    assert body["input"][0]["content"][-1]["type"] == "input_text"


def test_azure_sends_a_pdf_as_file_input(tmp_path, store, monkeypatch):
    AzureFoundry(store).connect(ENDPOINT, KEY)
    fake = FakeClient(post=FakeResponse(completed()))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: fake)
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF-1.4 fake")

    Providers(store, FakeChatGPT()).extract(path, "", options())

    attachment = fake.calls[0][2]["json"]["input"][0]["content"][0]
    assert attachment["type"] == "input_file" and attachment["filename"] == "invoice.pdf"
    assert attachment["file_data"].startswith("data:application/pdf;base64,")


def test_azure_model_listing_uses_the_resource_and_drops_unreadable_deployments(store, monkeypatch):
    AzureFoundry(store).connect(FOUNDRY_ENDPOINT, KEY)
    fake = FakeClient(get=FakeResponse({"data": [
        {"id": "invoice-reader"},
        {"id": "gpt-4.1-vision"},
        {"id": "text-embedding-3-large"},
        {"id": "my-whisper-deploy"},
        {"id": "tts-hd"},
    ]}))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: fake)

    assert Providers(store, FakeChatGPT()).models("azure") == [
        {"id": "invoice-reader", "name": "invoice-reader"},
        {"id": "gpt-4.1-vision", "name": "gpt-4.1-vision"},
    ]
    assert fake.calls[0][1] == FOUNDRY_ENDPOINT + "/openai/v1/models"
    assert fake.calls[0][2]["headers"] == {"api-key": KEY}


def test_azure_without_a_connection_is_refused_before_any_request(tmp_path, store, monkeypatch):
    sent = FakeClient()
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: sent)
    path = tmp_path / "invoice.txt"
    path.write_text("visible")

    with pytest.raises(ValueError, match="Connect Azure AI Foundry"):
        Providers(store, FakeChatGPT()).extract(path, "visible", options())
    with pytest.raises(ValueError, match="Connect Azure AI Foundry"):
        Providers(store, FakeChatGPT()).models("azure")
    assert sent.calls == []


@pytest.mark.parametrize("status,words", [(401, "rejected the credential"), (403, "cannot access"),
                                          (429, "rate limit"), (400, "rejected this model"), (503, "HTTP 503")])
def test_azure_http_failures_are_named_and_never_echo_the_key(tmp_path, store, monkeypatch, status, words):
    AzureFoundry(store).connect(ENDPOINT, KEY)
    fake = FakeClient(post=FakeResponse(status_code=status))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: fake)
    path = tmp_path / "invoice.txt"
    path.write_text("visible")

    with pytest.raises(ValueError) as error:
        Providers(store, FakeChatGPT()).extract(path, "visible", options())
    assert words in str(error.value)
    assert KEY not in str(error.value)


def test_an_incomplete_or_malformed_azure_answer_is_not_accepted(tmp_path, store, monkeypatch):
    AzureFoundry(store).connect(ENDPOINT, KEY)
    path = tmp_path / "invoice.txt"
    path.write_text("visible")
    for payload, words in ((dict(completed(), status="incomplete"), "incomplete"),
                           ({"status": "completed", "output": [{"content": [{"type": "output_text", "text": "{]"}]}]}, "invalid invoice structure")):
        monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: FakeClient(post=FakeResponse(payload)))
        with pytest.raises(ValueError, match=words):
            Providers(store, FakeChatGPT()).extract(path, "visible", options())


def test_an_azure_read_is_counted_and_priced_in_the_spend_ledger(tmp_path, store, monkeypatch):
    """The usage Azure returns flows into the same cost record Vertex reads produce."""
    AzureFoundry(store).connect(ENDPOINT, KEY)
    usage = {"input_tokens": 4000, "input_tokens_details": {"cached_tokens": 1000},
             "output_tokens": 1700, "output_tokens_details": {"reasoning_tokens": 200}, "total_tokens": 5700}
    fake = FakeClient(post=FakeResponse(completed(usage)))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: fake)
    path = tmp_path / "invoice.txt"
    path.write_text("visible")
    _, meta = Providers(store, FakeChatGPT()).extract(path, "visible", options())
    trace = [{"engine": "azure", "model": "invoice-reader", "status": "extracted", "usage": meta}]

    assert costs.usage_of(trace) == {"calls": 1, "input_tokens": 3000, "output_tokens": 1700, "cached_tokens": 1000}
    # A deployment name is operator chosen, so it is priced through INV_STUDIO_AI_PRICES, never guessed.
    unpriced = costs.record(trace, "invoice-reader", "2026-10-27")
    assert unpriced["priced"] is False and unpriced["usd"] is None and unpriced["input_tokens"] == 3000
    monkeypatch.setenv("INV_STUDIO_AI_PRICES", '{"invoice-reader": {"input": 2.0, "output": 8.0, "cached": 0.5}}')
    priced = costs.record(trace, "invoice-reader", "2026-10-27")
    assert priced["priced"] is True and priced["usd"] == "0.020100"
    assert priced["price_per_million"] == {"input": "2.0", "output": "8.0", "cached": "0.5"}
    assert costs.model_of(trace) == "invoice-reader"


def test_azure_is_connectable_through_the_api_by_an_administrator_only(client, owner, tenant):
    state = client.get("/api/state", headers=bearer(owner)).json()
    assert state["connections"]["azure"] is False
    assert state["azure_endpoint"] == ""

    refused = client.post("/api/connections/azure", headers=bearer(owner),
                          json={"endpoint": "https://evil.example.com", "api_key": KEY})
    assert refused.status_code == 400
    assert client.get("/api/state", headers=bearer(owner)).json()["connections"]["azure"] is False

    member = tenant.token("member-uid", "member@example.test")
    forbidden = client.post("/api/connections/azure", headers=bearer(member),
                            json={"endpoint": "acme.openai.azure.com", "api_key": KEY})
    assert forbidden.status_code == 403

    saved = client.post("/api/connections/azure", headers=bearer(owner),
                        json={"endpoint": "acme.openai.azure.com", "api_key": KEY})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"connected": True, "provider": "azure", "endpoint": ENDPOINT}

    state = client.get("/api/state", headers=bearer(member)).json()
    assert state["connections"]["azure"] is True
    # The endpoint is shown back for confirmation; the key never is.
    assert state["azure_endpoint"] == ENDPOINT
    assert KEY not in json.dumps(state)

    assert client.post("/api/settings", headers=bearer(owner),
                       json={"provider": "azure", "model": "invoice-reader", "ai_fallback": True}).status_code == 200
    assert client.get("/api/state", headers=bearer(member)).json()["settings"]["provider"] == "azure"

    assert client.delete("/api/connections/azure", headers=bearer(member)).status_code == 403
    removed = client.delete("/api/connections/azure", headers=bearer(owner))
    assert removed.status_code == 200
    assert client.get("/api/state", headers=bearer(owner)).json()["connections"]["azure"] is False
    with client.app.state.store.connection() as c:
        events = [row["event"] for row in c.execute("SELECT event FROM audit ORDER BY id")]
    assert "connection_connected" in events and "connection_disconnected" in events


def test_the_environment_makes_azure_the_hosted_managed_default(tmp_path, identity, tenant, monkeypatch):
    """Like VERTEX_PROJECT_ID today: the endpoint and key (from Secret Manager) plus a deployment name."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.setenv("INV_STUDIO_AZURE_ENDPOINT", FOUNDRY_ENDPOINT)
    monkeypatch.setenv("INV_STUDIO_AZURE_API_KEY", "env-supplied-key")
    monkeypatch.setenv("INV_STUDIO_AZURE_DEPLOYMENT", "gpt-5-invoices")
    app = create_app(tmp_path / "managed", identity=identity, store_type=Store)
    with TestClient(app) as client:
        token = tenant.token("owner-uid", "owner@example.test", roles=("admin",))
        state = client.get("/api/state", headers=bearer(token)).json()
        assert state["connections"]["azure"] is True
        assert state["azure_endpoint"] == FOUNDRY_ENDPOINT
        assert state["azure_deployment"] == "gpt-5-invoices"
        assert state["settings"]["provider"] == "azure" and state["settings"]["model"] == "gpt-5-invoices"
        assert "env-supplied-key" not in json.dumps(state)

        # Vertex, when configured, stays the managed default unless the deployment says otherwise.
        monkeypatch.setenv("VERTEX_PROJECT_ID", "live-project")
        settings = client.get("/api/state", headers=bearer(token)).json()["settings"]
        assert settings["provider"] == "vertex" and settings["model"] == "gemini-3.7-flash"
        monkeypatch.setenv("INV_STUDIO_DEFAULT_PROVIDER", "azure")
        settings = client.get("/api/state", headers=bearer(token)).json()["settings"]
        assert settings["provider"] == "azure" and settings["model"] == "gpt-5-invoices"

        # A model an administrator chose is never overridden by the managed default.
        assert client.post("/api/settings", headers=bearer(token),
                           json={"provider": "openai", "model": "gpt-5", "ai_fallback": True}).status_code == 200
        settings = client.get("/api/state", headers=bearer(token)).json()["settings"]
        assert settings["provider"] == "openai" and settings["model"] == "gpt-5"


def test_every_reader_in_the_shared_provider_list_is_offered_identically(client, owner):
    """The selectable set is one list: no reader exists only locally or only in the cloud."""
    assert "azure" in AI_PROVIDERS
    assert set(client.get("/api/state", headers=bearer(owner)).json()["connections"]) == set(AI_PROVIDERS)
    for provider in AI_PROVIDERS:
        # Selecting a reader is allowed for every entry; connecting it is configuration.
        assert client.post("/api/settings", headers=bearer(owner),
                           json={"provider": provider, "model": "", "ai_fallback": True}).status_code == 200
        assert client.get(f"/api/connections/{provider}/models", headers=bearer(owner)).status_code in (200, 400)
    assert client.get("/api/connections/not-a-provider/models", headers=bearer(owner)).status_code == 400
    assert client.post("/api/settings", headers=bearer(owner),
                       json={"provider": "not-a-provider", "model": "", "ai_fallback": True}).status_code == 400


def test_the_browser_offers_the_shared_provider_list_in_every_picker():
    markup = (ROOT / "app" / "static" / "index.html").read_text()
    script = (ROOT / "app" / "static" / "app.js").read_text()
    for picker in ("settings-provider", "upload-provider", "retry-provider"):
        start = markup.index(f'id="{picker}"')
        options_markup = markup[start:markup.index("</select>", start)]
        for provider in AI_PROVIDERS:
            assert f'value="{provider}"' in options_markup, f"{provider} missing from {picker}"
    assert 'data-connection-state="azure"' in markup
    assert 'data-provider="azure"' in markup
    assert "data-azure-endpoint" in markup
    # The reader list the client reasons about carries the same entries.
    listed = script[script.index("const AI_ENGINES = ["):].split("]")[0]
    for provider in AI_PROVIDERS:
        assert f'"{provider}"' in listed, f"{provider} missing from AI_ENGINES"
    assert "saveAzureConnection" in script
    assert HEADERS  # the mutation header the browser sends; kept imported so the fixture contract is visible here
