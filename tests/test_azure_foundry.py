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
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(sample_invoice())}]}],
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

    assert connection.save(connection.prepare(ENDPOINT, KEY, "reader")) == {
        "connected": True, "provider": "azure", "endpoint": ENDPOINT, "name": "reader", "route": "deployment"}
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
        connection.save(connection.prepare("https://evil.example.com", KEY))
    with pytest.raises(ValueError, match="API key"):
        connection.save(connection.prepare(ENDPOINT, "   "))
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
    connection.save(connection.prepare(ENDPOINT, KEY))
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
    AzureFoundry(store).save(AzureFoundry(store).prepare(endpoint, KEY))
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
    AzureFoundry(store).save(AzureFoundry(store).prepare(ENDPOINT, KEY))
    fake = FakeClient(post=FakeResponse(completed()))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: fake)
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF-1.4 fake")

    Providers(store, FakeChatGPT()).extract(path, "", options())

    attachment = fake.calls[0][2]["json"]["input"][0]["content"][0]
    assert attachment["type"] == "input_file" and attachment["filename"] == "invoice.pdf"
    assert attachment["file_data"].startswith("data:application/pdf;base64,")


def test_azure_model_listing_uses_the_resource_and_drops_unreadable_deployments(store, monkeypatch):
    AzureFoundry(store).save(AzureFoundry(store).prepare(FOUNDRY_ENDPOINT, KEY))
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
    AzureFoundry(store).save(AzureFoundry(store).prepare(ENDPOINT, KEY))
    fake = FakeClient(post=FakeResponse(status_code=status))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: fake)
    path = tmp_path / "invoice.txt"
    path.write_text("visible")

    with pytest.raises(ValueError) as error:
        Providers(store, FakeChatGPT()).extract(path, "visible", options())
    assert words in str(error.value)
    assert KEY not in str(error.value)


def test_an_incomplete_or_malformed_azure_answer_is_not_accepted(tmp_path, store, monkeypatch):
    AzureFoundry(store).save(AzureFoundry(store).prepare(ENDPOINT, KEY))
    path = tmp_path / "invoice.txt"
    path.write_text("visible")
    for payload, words in ((dict(completed(), status="incomplete"), "incomplete"),
                           ({"status": "completed", "output": [{"content": [{"type": "output_text", "text": "{]"}]}]}, "invalid invoice structure")):
        monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: FakeClient(post=FakeResponse(payload)))
        with pytest.raises(ValueError, match=words):
            Providers(store, FakeChatGPT()).extract(path, "visible", options())


def test_an_azure_read_is_counted_and_priced_in_the_spend_ledger(tmp_path, store, monkeypatch):
    """The usage Azure returns flows into the same cost record Vertex reads produce."""
    AzureFoundry(store).save(AzureFoundry(store).prepare(ENDPOINT, KEY))
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


def test_azure_is_connectable_through_the_api_by_an_administrator_only(client, owner, tenant, monkeypatch):
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

    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: FakeClient(post=FakeResponse(completed())))
    saved = client.post("/api/connections/azure", headers=bearer(owner),
                        json={"endpoint": "acme.openai.azure.com", "name": "invoice-reader", "api_key": KEY})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"connected": True, "provider": "azure", "endpoint": ENDPOINT, "name": "invoice-reader", "route": "deployment"}

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


# The connection test (ac6): a real call first, saved only on a pass. Microsoft Learn documents the agent call; it is
# unproven against a real agent until someone runs it.

PROJECT = "https://acme.services.ai.azure.com/api/projects/invoices"


def said(text="OK"):
    return FakeResponse({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]})


def connecting(monkeypatch, response):
    fake = FakeClient(post=response)
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: fake)
    return fake


def test_a_saved_connection_stays_when_a_new_ones_test_fails(client, owner, monkeypatch):
    connecting(monkeypatch, said())
    first = client.post("/api/connections/azure", headers=bearer(owner), json={"endpoint": ENDPOINT, "name": "reader", "api_key": KEY})
    assert first.status_code == 200, first.text
    for response in (FakeResponse(status_code=401), FakeResponse(status_code=404), FakeResponse({"status": "failed"}), said("  ")):
        connecting(monkeypatch, response)
        refused = client.post("/api/connections/azure", headers=bearer(owner),
                              json={"endpoint": "https://other.openai.azure.com", "name": "other", "api_key": "another-key-012345"})
        assert refused.status_code == 400 and "Nothing was saved" in refused.json()["detail"]
        assert "another-key" not in refused.text
        assert client.get("/api/state", headers=bearer(owner)).json()["azure_endpoint"] == ENDPOINT


def test_a_failed_first_test_saves_nothing(client, owner, monkeypatch):
    connecting(monkeypatch, FakeResponse(status_code=500))
    assert client.post("/api/connections/azure", headers=bearer(owner),
                       json={"endpoint": ENDPOINT, "name": "reader", "api_key": KEY}).status_code == 400
    assert client.get("/api/state", headers=bearer(owner)).json()["connections"]["azure"] is False


def test_the_test_is_a_real_call_needing_a_name(client, owner, monkeypatch):
    fake = connecting(monkeypatch, said())
    nameless = client.post("/api/connections/azure", headers=bearer(owner), json={"endpoint": ENDPOINT, "api_key": KEY})
    assert nameless.status_code == 400 and fake.calls == []
    assert client.post("/api/connections/azure", headers=bearer(owner),
                       json={"endpoint": ENDPOINT, "name": "reader", "api_key": KEY}).status_code == 200
    [(method, url, sent)] = fake.calls
    assert method == "POST" and url == ENDPOINT + "/openai/v1/responses"
    assert sent["headers"] == {"api-key": KEY} and sent["json"]["model"] == "reader" and sent["json"]["store"] is False


def test_an_agent_is_called_by_reference_with_managed_identity_and_no_key(client, owner, monkeypatch):
    scopes = []
    monkeypatch.setattr(azure_module, "bearer_token", lambda scope=None: scopes.append(scope) or "entra-token")
    fake = connecting(monkeypatch, said())
    saved = client.post("/api/connections/azure", headers=bearer(owner),
                        json={"endpoint": PROJECT, "name": "invoice-agent", "auth": "managed_identity"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["route"] == "agent" and saved.json()["endpoint"] == PROJECT
    [(_, url, sent)] = fake.calls
    assert url == PROJECT + "/openai/v1/responses"
    assert sent["json"] == {"input": "Reply with the word OK.", "store": False,
                            "agent_reference": {"name": "invoice-agent", "type": "agent_reference"}}
    assert sent["headers"] == {"Authorization": "Bearer entra-token"} and scopes == ["https://ai.azure.com/.default"]


def test_a_read_goes_to_the_agent_named_with_the_schema_in_its_input(store, monkeypatch):
    monkeypatch.setattr(azure_module, "bearer_token", lambda scope=None: "entra-token")
    providers = Providers(store, FakeChatGPT())
    providers.azure.save(providers.azure.prepare(PROJECT, None, "invoice-agent", "managed_identity"))
    fake = connecting(monkeypatch, FakeResponse(completed()))
    invoice, _ = providers.extract(Path(ROOT / "samples" / "invoice.pdf"), "text", options(model="invoice-agent"))
    assert invoice.number
    [(_, url, sent)] = fake.calls
    assert url == PROJECT + "/openai/v1/responses"
    assert sent["json"]["agent_reference"]["name"] == "invoice-agent"
    assert not {"model", "instructions", "text", "tools"} & set(sent["json"])
    assert "matching this JSON schema" in sent["json"]["input"][0]["content"][0]["text"]


TOOL_CALL = {"type": "web_search_call", "status": "completed"}


def test_an_agent_reply_holding_a_tool_call_is_refused_and_its_reasoning_is_not(store, monkeypatch):
    monkeypatch.setattr(azure_module, "bearer_token", lambda scope=None: "entra-token")
    providers = Providers(store, FakeChatGPT())
    providers.azure.save(providers.azure.prepare(PROJECT, None, "invoice-agent", "managed_identity"))
    sample = ROOT / "samples" / "invoice.pdf"
    connecting(monkeypatch, FakeResponse(dict(completed(), output=[TOOL_CALL, *completed()["output"]])))
    with pytest.raises(ValueError, match="must have no tools"):
        providers.extract(Path(sample), "text", options(model="invoice-agent"))
    connecting(monkeypatch, FakeResponse(dict(completed(), output=[{"type": "reasoning"}, *completed()["output"]])))
    assert providers.extract(Path(sample), "text", options(model="invoice-agent"))[0].number


def test_an_agent_that_used_a_tool_fails_the_connection_test_and_saves_nothing(client, owner, monkeypatch):
    monkeypatch.setattr(azure_module, "bearer_token", lambda scope=None: "entra-token")
    connecting(monkeypatch, FakeResponse(dict(said().json(), output=[TOOL_CALL, *said().json()["output"]])))
    refused = client.post("/api/connections/azure", headers=bearer(owner),
                          json={"endpoint": PROJECT, "name": "invoice-agent", "auth": "managed_identity"})
    assert refused.status_code == 400 and "must have no tools" in refused.json()["detail"]
    assert client.get("/api/state", headers=bearer(owner)).json()["connections"]["azure"] is False


def test_a_refused_endpoint_in_the_environment_turns_the_ai_off_and_not_the_app(client, owner, monkeypatch):
    monkeypatch.setenv(azure_module.ENV_ENDPOINT, "https://models.example.com")
    state = client.get("/api/state", headers=bearer(owner))
    assert state.status_code == 200 and azure_module.ENV_ENDPOINT in state.json()["azure_refused"]
    assert state.json()["connections"]["azure"] is False


GATEWAY = "citadel-gw.contoso.example"


def test_a_saved_gateway_connection_stops_working_when_the_deployment_stops_naming_it(store, monkeypatch):
    monkeypatch.setenv(azure_module.ENV_GATEWAY, GATEWAY)
    foundry = AzureFoundry(store)
    foundry.save(foundry.prepare(f"https://{GATEWAY}", KEY))
    assert azure_module._saved(store)
    monkeypatch.delenv(azure_module.ENV_GATEWAY)
    assert azure_module._saved(store) is None


@pytest.mark.parametrize("host", ["127.0.0.1", "0x7f.0.0.1", "10.0.0.1", "gw.example.com:8443", "gw.example.com/path"])
def test_an_address_port_or_path_is_no_gateway(host):
    assert not azure_module.valid_gateway(host)


def test_a_new_connection_replaces_the_model_a_read_would_name(client, owner, monkeypatch):
    connecting(monkeypatch, said())
    connect = {"endpoint": ENDPOINT, "api_key": KEY}
    assert client.post("/api/connections/azure", headers=bearer(owner), json={**connect, "name": "reader"}).status_code == 200
    assert client.post("/api/settings", headers=bearer(owner), json={"provider": "azure", "model": "reader", "ai_fallback": True}).status_code == 200
    assert client.post("/api/connections/azure", headers=bearer(owner), json={**connect, "name": "reader-2"}).status_code == 200
    assert client.get("/api/state", headers=bearer(owner)).json()["settings"]["model"] == "reader-2"


def test_a_key_refused_on_an_agent_names_managed_identity(client, owner, monkeypatch):
    connecting(monkeypatch, FakeResponse(status_code=401))
    refused = client.post("/api/connections/azure", headers=bearer(owner), json={"endpoint": PROJECT, "name": "invoice-agent", "api_key": KEY})
    assert refused.status_code == 400 and "managed identity" in refused.json()["detail"] and KEY not in refused.text


def test_managed_identity_with_no_token_says_which_role_and_saves_nothing(client, owner, monkeypatch):
    def no_token(scope=None):
        raise RuntimeError("no identity here")
    monkeypatch.setattr(azure_module, "bearer_token", no_token)
    fake = connecting(monkeypatch, said())
    refused = client.post("/api/connections/azure", headers=bearer(owner),
                          json={"endpoint": PROJECT, "name": "invoice-agent", "auth": "managed_identity"})
    assert refused.status_code == 400 and "Foundry Agent Consumer" in refused.json()["detail"]
    assert fake.calls == [] and client.get("/api/state", headers=bearer(owner)).json()["connections"]["azure"] is False


@pytest.mark.parametrize("endpoint", [
    "https://evil.example.com/api/projects/x", "https://acme.openai.azure.com/api/projects/x",
    "https://acme.services.ai.azure.com/api/projects/x/extra", "https://acme.services.ai.azure.com/other",
])
def test_the_allow_list_holds_for_a_project_endpoint(endpoint):
    with pytest.raises(ValueError):
        normalise_endpoint(endpoint, "")


def test_the_key_and_test_are_kept_out_of_the_audit(client, owner, app, monkeypatch):
    connecting(monkeypatch, said())
    client.post("/api/connections/azure", headers=bearer(owner), json={"endpoint": ENDPOINT, "name": "reader", "api_key": KEY})
    with app.state.store.connection() as c:
        logged = json.dumps([tuple(r) for r in c.execute("SELECT * FROM audit").fetchall()])
    assert "connection_connected" in logged and KEY not in logged
