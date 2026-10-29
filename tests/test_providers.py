"""The AI provider adapters against mocked HTTP: no key, no network, no live inference.

The fakes here are shared with tests/test_azure_foundry.py.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.providers as provider_module
from app.models import ProcessingOptions
from app.providers import Providers, safe_error
from app.store import Store
from app.subscriptions import ClaudeSubscription


ROOT = Path(__file__).resolve().parents[1]


def sample_invoice():
    return json.loads((ROOT / "samples" / "invoice.json").read_text())


class FakeResponse:
    def __init__(self, payload=None, status_code=200, lines=None):
        self.payload = payload or {}
        self.status_code = status_code
        self._lines = lines or []

    def json(self):
        return self.payload

    def iter_lines(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeClient:
    def __init__(self, *, post=None, get=None, stream=None):
        self.post_response = post or FakeResponse()
        self.get_response = get or FakeResponse()
        self.stream_response = stream or FakeResponse()
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.post_response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.get_response

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.stream_response


class FakeChatGPT:
    def token(self):
        return "chatgpt-access-token"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def options(provider="openai"):
    return ProcessingOptions(engine="ai", ai_fallback=True, provider=provider, model="test-model")


def test_openai_mocked_success_uses_strict_schema_and_disables_storage(tmp_path, store, monkeypatch):
    store.secret("openai", "openai-test-secret")
    payload = {"status": "completed", "usage": {"input_tokens": 12, "output_tokens": 7},
               "output": [{"content": [{"type": "output_text", "text": json.dumps(sample_invoice())}]}]}
    fake = FakeClient(post=FakeResponse(payload))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: fake)
    path = tmp_path / "invoice.txt"
    path.write_text("visible invoice text")

    invoice, usage = Providers(store, FakeChatGPT()).extract(path, "visible invoice text", options())

    assert invoice.number == "DEMO-2026-001"
    assert usage == {"input_tokens": 12, "output_tokens": 7}
    _, url, request = fake.calls[0]
    assert url == "https://api.openai.com/v1/responses"
    assert request["headers"] == {"Authorization": "Bearer openai-test-secret"}
    assert request["json"]["store"] is False
    assert request["json"]["text"]["format"]["strict"] is True


def test_openai_model_listing_offers_only_readers(store, monkeypatch):
    store.secret("openai", "openai-test-secret")
    fake = FakeClient(get=FakeResponse({"data": [
        {"id": "gpt-5"}, {"id": "o3"}, {"id": "gpt-4o-realtime-preview"}, {"id": "text-embedding-3-large"}]}))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *a, **k: fake)
    assert Providers(store, FakeChatGPT()).models("openai") == [
        {"id": "gpt-5", "name": "gpt-5"}, {"id": "o3", "name": "o3"}]


def test_chatgpt_model_listing_reads_the_resource_server_shape(store, monkeypatch):
    """A plan token lists models from api.openai.com, which answers {"data": [...]}.

    Parsing a different shape here returned an empty list, so the picker offered no
    model, no model could be selected and the connection could never be used.
    """
    fake = FakeClient(get=FakeResponse({"data": [
        {"id": "gpt-5", "display_name": "GPT-5"},
        {"id": "o3"},
        {"id": "gpt-4o-realtime-preview"},
        {"id": "text-embedding-3-large"},
    ]}))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *args, **kwargs: fake)

    assert Providers(store, FakeChatGPT()).models("chatgpt") == [
        {"id": "gpt-5", "name": "GPT-5"},
        {"id": "o3", "name": "o3"},
    ]
    _, url, request = fake.calls[0]
    assert url == "https://api.openai.com/v1/models"
    assert request["headers"] == {"Authorization": "Bearer chatgpt-access-token"}


def test_model_listing_survives_an_entry_without_an_id(store, monkeypatch):
    store.secret("anthropic", "anthropic-test-secret")
    fake = FakeClient(get=FakeResponse({"data": [{"id": "claude-x"}, {"no_id": True}, "not-an-object"]}))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *args, **kwargs: fake)

    assert Providers(store, FakeChatGPT()).models("anthropic") == [{"id": "claude-x", "name": "claude-x"}]


def test_a_rejected_listing_reports_the_safe_error(store, monkeypatch):
    store.secret("anthropic", "anthropic-test-secret")
    fake = FakeClient(get=FakeResponse(status_code=429))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *args, **kwargs: fake)
    with pytest.raises(ValueError, match="rate limit"):
        Providers(store, FakeChatGPT()).models("anthropic")


def test_a_failed_claude_code_run_names_the_reason_the_cli_reported():
    """The CLI exits non-zero with a reason; reporting only "failed" left it unactionable."""
    assert "Reconnect its setup token" in provider_module.local_claude_error(
        json.dumps({"terminal_reason": "api_error", "is_error": True})
    )
    assert provider_module.local_claude_error(json.dumps({"api_error_status": 429})) == safe_error(429)
    assert provider_module.local_claude_error(json.dumps({"api_error_status": 401})) == safe_error(401)
    assert "declined to read" in provider_module.local_claude_error(json.dumps({"terminal_reason": "refusal"}))
    # An unparsable or unfamiliar report still produces one safe, generic message.
    assert provider_module.local_claude_error("not json") == provider_module.CLAUDE_FAILED
    assert provider_module.local_claude_error("[]") == provider_module.CLAUDE_FAILED
    assert provider_module.local_claude_error(json.dumps({"terminal_reason": "something_new"})) == provider_module.CLAUDE_FAILED


def test_a_failed_claude_code_run_surfaces_that_reason_to_the_caller(tmp_path, store, monkeypatch):
    ClaudeSubscription(store).connect("sk-ant-oat01-" + "s" * 64)
    monkeypatch.setattr(provider_module.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(
        provider_module.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=1, stdout=json.dumps({"terminal_reason": "api_error"}), stderr=""
        ),
    )
    path = tmp_path / "invoice.txt"
    path.write_text("visible invoice")

    with pytest.raises(ValueError, match="Reconnect its setup token"):
        Providers(store, FakeChatGPT()).extract(path, "visible invoice", options("claude_local"))


def test_connecting_and_removing_an_api_key_is_audited_without_the_key(client, owner, monkeypatch):
    from tests.conftest import bearer
    key = "sk-test-audit-0123456789"
    monkeypatch.setattr(Providers, "verify_key", lambda self, provider, value: True)
    assert client.post("/api/connections/openai", headers=bearer(owner), json={"api_key": key}).status_code == 200
    assert client.delete("/api/connections/openai", headers=bearer(owner)).status_code == 200
    with client.app.state.store.connection() as c:
        rows = [(row["event"], json.loads(row["payload"])) for row in c.execute("SELECT event,payload FROM audit ORDER BY id")]
    events = [(event, payload["provider"]) for event, payload in rows if event.startswith("connection_")]
    assert events == [("connection_connected", "openai"), ("connection_disconnected", "openai")]
    assert key not in json.dumps(rows)


@pytest.mark.parametrize("model", ["-p", "--dangerously-skip-permissions", ".hidden"])
def test_a_model_name_cannot_start_like_a_cli_option(model):
    from pydantic import ValidationError
    from app.main import Settings
    with pytest.raises(ValidationError):
        ProcessingOptions(model=model)
    with pytest.raises(ValidationError):
        Settings(model=model)


def test_ordinary_model_names_still_pass():
    for model in ("", "claude-sonnet-5-5", "gpt-5.1", "models/gemini:latest"):
        assert ProcessingOptions(model=model).model == model
