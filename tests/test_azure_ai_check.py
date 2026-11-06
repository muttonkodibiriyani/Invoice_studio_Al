"""Azure AI Foundry before go-live: the endpoint and key are configuration, and scripts/azure_ai_check.py proves a
configured deployment reads invoices through the app's own code.

Everything here runs on mocked HTTP (no key, no network) except the one azure_live test. That test calls the real
deployment the environment or .env names, so `pytest -q` leaves it out:

    .venv/bin/pytest -m azure_live -rs      once the endpoint, key and deployment are received
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import app.azure_foundry as azure_module
import app.providers as provider_module
from app.azure_foundry import AzureFoundry
from app.models import ProcessingOptions
from app.providers import Providers
from app.store import Store
from tests.test_providers import FakeChatGPT, FakeResponse, sample_invoice

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "azure_ai_check.py"
spec = importlib.util.spec_from_file_location("azure_ai_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
ENDPOINT = "https://acme.openai.azure.com"
FOUNDRY_ENDPOINT = "https://acme.services.ai.azure.com"
# Made-up test values, not credentials.
KEY = "test-key-from-env"
ROTATED = "test-key-rotated"
UI_KEY = "test-key-from-ui"
DEPLOYMENT = "invoice-reader"
AZURE_ENV = ("INV_STUDIO_AZURE_ENDPOINT", "INV_STUDIO_AZURE_API_KEY", "INV_STUDIO_AZURE_DEPLOYMENT",
             "INV_STUDIO_AZURE_AUTH", "INV_STUDIO_AZURE_TOKEN_SCOPE", "INV_STUDIO_AI_PROVIDERS")


@pytest.fixture(autouse=True)
def _no_azure_environment(request, monkeypatch):
    """Each test sets the Azure configuration it means. The live test keeps the real one."""
    if request.node.get_closest_marker("azure_live"):
        return
    for name in AZURE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def environ():
    """load_env_file writes os.environ directly, like run.sh exports: put it back afterwards."""
    saved = os.environ.copy()
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def no_env_file(tmp_path):
    """An empty settings file, so the check never reads the developer's own .env."""
    path = tmp_path / "empty.env"
    path.write_text("")
    return str(path)


def configure(monkeypatch, endpoint=ENDPOINT, key=KEY, deployment=DEPLOYMENT):
    monkeypatch.setenv("INV_STUDIO_AZURE_ENDPOINT", endpoint)
    if key is not None:
        monkeypatch.setenv("INV_STUDIO_AZURE_API_KEY", key)
    if deployment is not None:
        monkeypatch.setenv("INV_STUDIO_AZURE_DEPLOYMENT", deployment)


def answer(invoice=None):
    """A completed Responses answer carrying an invoice: what a deployment that read the sample returns."""
    text = json.dumps(sample_invoice() if invoice is None else invoice)
    return FakeResponse({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                         "usage": {"input_tokens": 1800, "output_tokens": 400}})


def kind(body):
    """Which request this is: the check's probe, or a read of text, a PDF or an image."""
    if isinstance(body["input"], str):
        return "probe"
    types = {part["type"] for part in body["input"][0]["content"]}
    return "pdf" if "input_file" in types else "image" if "input_image" in types else "text"


class FakeFoundry:
    """Stands in for httpx.Client and the Azure resource behind it: records each request, answers per request kind."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None, **kwargs):
        self.calls.append({"url": url, "headers": headers, "body": json, "kind": kind(json)})
        reply = self.answers.get(kind(json), answer())
        if isinstance(reply, Exception):
            raise reply
        return reply

    def get(self, url, **kwargs):
        raise AssertionError("the check and a read make no GET request")


@pytest.fixture
def foundry(monkeypatch):
    def install(**answers):
        fake = FakeFoundry(**answers)
        monkeypatch.setattr(provider_module.httpx, "Client", fake)
        return fake
    return install


def read(providers, tmp_path, deployment=DEPLOYMENT):
    """One AI read the way a job makes it (main.py hands providers.extract to every job)."""
    path = tmp_path / "invoice.txt"
    path.write_text("Invoice No: DEMO-2026-001")
    options = ProcessingOptions(engine="ai", ai_fallback=True, provider="azure", model=deployment)
    return providers.extract(path, "Invoice No: DEMO-2026-001", options)


def app_providers(tmp_path):
    return Providers(Store(tmp_path / "data"), FakeChatGPT())


# ---- The endpoint and key are configuration -------------------------------------------------------------------

@pytest.mark.parametrize("supplied,origin", [
    (ENDPOINT, ENDPOINT),
    ("acme.openai.azure.com", ENDPOINT),
    (FOUNDRY_ENDPOINT + "/openai/v1/", FOUNDRY_ENDPOINT),
    ("https://acme.cognitiveservices.azure.com/openai", "https://acme.cognitiveservices.azure.com"),
])
def test_the_configured_endpoint_key_and_deployment_are_what_a_read_sends(tmp_path, monkeypatch, foundry,
                                                                            supplied, origin):
    """No UI step: the three environment values alone connect Azure, and they are exactly what goes on the wire."""
    configure(monkeypatch, endpoint=supplied)
    fake = foundry()

    invoice, _ = read(app_providers(tmp_path), tmp_path)

    assert invoice.number == "DEMO-2026-001"
    [call] = fake.calls
    assert call["url"] == origin + "/openai/v1/responses"
    assert call["headers"] == {"api-key": KEY}
    assert call["body"]["model"] == DEPLOYMENT


def test_a_changed_key_is_used_by_the_next_read_and_a_ui_connection_wins_over_the_environment(tmp_path, monkeypatch,
                                                                                            foundry):
    """Rotating the key needs no restart and no reconnect: the app reads the configuration on every call."""
    configure(monkeypatch)
    providers = app_providers(tmp_path)  # one instance for every read, as the running app holds it
    fake = foundry()

    read(providers, tmp_path)
    monkeypatch.setenv("INV_STUDIO_AZURE_API_KEY", ROTATED)
    read(providers, tmp_path)
    providers.azure.save(providers.azure.prepare(FOUNDRY_ENDPOINT, UI_KEY))  # an administrator's Engines & AI connection
    read(providers, tmp_path)
    providers.azure.disconnect()
    read(providers, tmp_path)

    assert [(c["url"], c["headers"]) for c in fake.calls] == [
        (ENDPOINT + "/openai/v1/responses", {"api-key": KEY}),
        (ENDPOINT + "/openai/v1/responses", {"api-key": ROTATED}),
        (FOUNDRY_ENDPOINT + "/openai/v1/responses", {"api-key": UI_KEY}),
        (ENDPOINT + "/openai/v1/responses", {"api-key": ROTATED}),
    ]


@pytest.mark.parametrize("endpoint", [
    "https://citadel-gw.azure-api.net",          # an API Management gateway: not a Foundry resource host
    "http://acme.openai.azure.com",              # the key would travel in clear
    "https://10.0.0.5",                          # a private address
    "https://169.254.169.254",                   # instance metadata
    "https://user:pass@acme.openai.azure.com",
    "https://acme.openai.azure.com.evil.example.com",
])
def test_a_configured_endpoint_the_app_refuses_sends_nothing(tmp_path, monkeypatch, foundry, endpoint):
    configure(monkeypatch, endpoint=endpoint)
    fake = foundry()
    with pytest.raises(ValueError):
        read(app_providers(tmp_path), tmp_path)
    assert fake.calls == []


def test_an_endpoint_without_a_key_is_not_a_connection(tmp_path, monkeypatch, foundry):
    configure(monkeypatch, key=None)
    fake = foundry()
    assert AzureFoundry(Store(tmp_path / "data")).configured() is False
    with pytest.raises(ValueError, match="Connect Azure AI Foundry"):
        read(app_providers(tmp_path), tmp_path)
    assert fake.calls == []


# ---- scripts/azure_ai_check.py --------------------------------------------------------------------------------

def test_the_check_passes_when_the_deployment_reads_every_sample(monkeypatch, foundry, no_env_file, capsys):
    configure(monkeypatch)
    fake = foundry()

    assert check.main(["--env-file", no_env_file]) == 0

    report = capsys.readouterr().out
    assert [c["kind"] for c in fake.calls] == ["probe", "text", "pdf", "image"]
    assert all(c["url"] == ENDPOINT + "/openai/v1/responses" for c in fake.calls)
    assert all(c["headers"] == {"api-key": KEY} for c in fake.calls)
    assert all(c["body"]["model"] == DEPLOYMENT and c["body"]["store"] is False for c in fake.calls)
    # The reads are the app's own request: the strict invoice schema, the synthetic samples only.
    text, pdf, image = (c["body"] for c in fake.calls[1:])
    assert text["text"]["format"]["strict"] is True
    assert "DEMO-2026-001" in text["input"][0]["content"][-1]["text"]
    # The PDF and the image travel with no text beside them, so the number can only come from the file.
    assert pdf["input"][0]["content"][0]["file_data"].startswith("data:application/pdf;base64,")
    assert pdf["input"][0]["content"][-1]["text"].endswith("Document text, if available:\n")
    assert image["input"][0]["content"][0]["image_url"].startswith("data:image/png;base64,")
    assert "Every step passed" in report and report.count(" ok ") == 5
    assert KEY not in report


def test_text_only_stops_after_the_text_read(monkeypatch, foundry, no_env_file):
    configure(monkeypatch)
    fake = foundry()
    assert check.main(["--env-file", no_env_file, "--text-only"]) == 0
    assert [c["kind"] for c in fake.calls] == ["probe", "text"]


def test_the_deployment_can_be_named_on_the_command_line(monkeypatch, foundry, no_env_file):
    configure(monkeypatch, deployment=None)
    fake = foundry()
    assert check.main(["--env-file", no_env_file, "--deployment", "gpt-41-invoices", "--text-only"]) == 0
    assert {c["body"]["model"] for c in fake.calls} == {"gpt-41-invoices"}


def test_nothing_configured_is_reported_as_such_and_calls_nothing(foundry, no_env_file, capsys):
    fake = foundry()
    assert check.main(["--env-file", no_env_file]) == 2
    report = capsys.readouterr().out
    assert "missing" in report and "INV_STUDIO_AZURE_ENDPOINT" in report and "INV_STUDIO_AZURE_API_KEY" in report
    assert "Nothing to check yet" in report
    assert fake.calls == []


@pytest.mark.parametrize("endpoint,says", [
    ("https://citadel-gw.azure-api.net", "API Management gateway"),
    ("http://acme.openai.azure.com", "is refused"),
    ("https://10.0.0.5", "is refused"),
])
def test_a_refused_endpoint_fails_the_check_before_any_request(monkeypatch, foundry, no_env_file, capsys,
                                                               endpoint, says):
    configure(monkeypatch, endpoint=endpoint)
    fake = foundry()
    assert check.main(["--env-file", no_env_file]) == 1
    report = capsys.readouterr().out
    assert says in report and "keeps working with AI off" in report
    assert fake.calls == []


def test_a_deployment_name_the_app_cannot_send_fails_the_configuration(monkeypatch, foundry, no_env_file, capsys):
    configure(monkeypatch, deployment="bad name;drop")
    fake = foundry()
    assert check.main(["--env-file", no_env_file]) == 1
    assert "is not one the app can send" in capsys.readouterr().out
    assert fake.calls == []


def test_a_deployment_that_only_allows_other_providers_fails_the_configuration(monkeypatch, foundry, no_env_file,
                                                                              capsys):
    configure(monkeypatch)
    monkeypatch.setenv("INV_STUDIO_AI_PROVIDERS", "vertex")
    fake = foundry()
    assert check.main(["--env-file", no_env_file]) == 1
    assert "add azure to INV_STUDIO_AI_PROVIDERS" in capsys.readouterr().out
    assert fake.calls == []


@pytest.mark.parametrize("status,code,says", [
    (401, "", "the key was refused"),
    (401, "401", "the key was refused"),  # Azure's own 401 body: the code repeats the status
    (403, "", "Cognitive Services OpenAI User"),
    (404, "DeploymentNotFound", "no such deployment"),
    (400, "OperationNotSupported", "not a chat model that serves the Responses API"),
    (429, "", "rate limit"),
    (503, "", "server error"),
    (307, "", "redirected"),
])
def test_a_refused_connection_names_the_reason_and_spends_nothing_more(monkeypatch, foundry, no_env_file, capsys,
                                                                       status, code, says):
    configure(monkeypatch)
    payload = {"error": {"code": code, "message": f"echo {KEY}"}} if code else {}
    fake = foundry(probe=FakeResponse(payload, status_code=status))
    assert check.main(["--env-file", no_env_file]) == 1
    report = capsys.readouterr().out
    assert f"HTTP {status}" in report and says in report
    if code and code != str(status):
        assert code in report
    assert f"HTTP {status} {status}" not in report
    assert [c["kind"] for c in fake.calls] == ["probe"]  # the reads are skipped, not sent
    assert KEY not in report  # Azure's own message is never shown, only its code


@pytest.mark.parametrize("error,says", [
    (httpx.ConnectTimeout("timed out"), "allow-out-ai"),
    (httpx.ConnectError("[Errno -2] Name or service not known"), "does not resolve"),
    (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "SSL_CERT_FILE"),
    (httpx.ReadTimeout("read timed out"), "did not answer in time"),
])
def test_a_network_failure_names_what_to_look_at(monkeypatch, foundry, no_env_file, capsys, error, says):
    configure(monkeypatch)
    foundry(probe=error)
    assert check.main(["--env-file", no_env_file]) == 1
    assert says in capsys.readouterr().out


def test_a_deployment_that_answers_but_cannot_read_a_document_fails_the_file_steps(monkeypatch, foundry,
                                                                                  no_env_file, capsys):
    configure(monkeypatch)
    nothing_read = {**sample_invoice(), "number": None}
    foundry(pdf=FakeResponse(status_code=400), image=answer(nothing_read))
    assert check.main(["--env-file", no_env_file]) == 1
    lines = {line.split()[0] + " " + line.split()[1]: line for line in capsys.readouterr().out.splitlines()
             if line.startswith("  read:")}
    assert " ok " in lines["read: text"]
    assert "FAILED" in lines["read: PDF"] and "does not accept a PDF as a file input" in lines["read: PDF"]
    assert "FAILED" in lines["read: image"] and "did not read the document" in lines["read: image"]


def test_an_incomplete_or_schema_breaking_answer_fails_the_read(monkeypatch, foundry, no_env_file, capsys):
    configure(monkeypatch)
    broken = FakeResponse({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "{]"}]}]})
    foundry(text=FakeResponse({"status": "incomplete"}), pdf=broken)
    assert check.main(["--env-file", no_env_file]) == 1
    report = capsys.readouterr().out
    assert "output-token cap" in report and "did not follow the invoice schema" in report


def test_managed_identity_needs_no_key_and_sends_the_app_identity_token(monkeypatch, foundry, no_env_file, capsys):
    """The InfoSec preference: no key anywhere, an Entra token for the app's identity on every request."""
    configure(monkeypatch, key=None)
    monkeypatch.setenv("INV_STUDIO_AZURE_AUTH", "managed_identity")
    monkeypatch.setattr(azure_module, "bearer_token", lambda scope=None: "entra-token-for-the-app")
    fake = foundry()
    assert check.main(["--env-file", no_env_file, "--text-only"]) == 0
    assert "auth managed identity" in capsys.readouterr().out
    assert all(c["headers"] == {"Authorization": "Bearer entra-token-for-the-app"} for c in fake.calls)


def test_the_check_keeps_no_copy_of_the_key(monkeypatch, foundry, no_env_file):
    """It never saves a connection: it tests the one the environment supplies, from a scratch store it deletes."""
    configure(monkeypatch)
    foundry()
    roots = []
    monkeypatch.setattr(check, "Store", lambda root: roots.append(root) or Store(root))
    assert check.main(["--env-file", no_env_file, "--text-only"]) == 0
    [root] = roots
    assert root.parent.name.startswith("inv-azure-check-")
    assert not root.exists()  # removed with its vault key and database, so nothing outlives the run


def test_settings_are_read_from_a_file_the_way_run_sh_reads_dot_env(tmp_path, environ):
    for name in AZURE_ENV:
        environ.pop(name, None)
    environ["INV_STUDIO_AZURE_DEPLOYMENT"] = "already-set-wins"
    path = tmp_path / "azure.env"
    path.write_text("# a comment\r\n"
                    'INV_STUDIO_AZURE_ENDPOINT="https://acme.openai.azure.com"\r\n'
                    "export INV_STUDIO_AZURE_API_KEY='quoted-test-key'\n"
                    "INV_STUDIO_AZURE_DEPLOYMENT=from-the-file\n"
                    "INV_STUDIO_AZURE_AUTH=\n"
                    "not a setting\n"
                    "lower_case=ignored\n")

    loaded = check.load_env_file(path)

    assert loaded == ["INV_STUDIO_AZURE_ENDPOINT", "INV_STUDIO_AZURE_API_KEY"]
    assert environ["INV_STUDIO_AZURE_ENDPOINT"] == "https://acme.openai.azure.com"
    assert environ["INV_STUDIO_AZURE_API_KEY"] == "quoted-test-key"
    assert environ["INV_STUDIO_AZURE_DEPLOYMENT"] == "already-set-wins"
    assert "INV_STUDIO_AZURE_AUTH" not in environ and "lower_case" not in environ


def test_a_named_settings_file_that_does_not_exist_is_refused(tmp_path, capsys):
    assert check.main(["--env-file", str(tmp_path / "missing.env")]) == 2
    assert "No such file" in capsys.readouterr().out


def test_the_check_runs_as_a_script_and_exits_2_when_nothing_is_configured(tmp_path, no_env_file):
    env = {k: v for k, v in os.environ.items() if k not in AZURE_ENV}
    result = subprocess.run([sys.executable, str(SCRIPT), "--env-file", no_env_file], capture_output=True,
                            text=True, timeout=120, cwd=tmp_path, env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Nothing to check yet" in result.stdout


# ---- The real deployment -------------------------------------------------------------------------------------

@pytest.mark.azure_live
def test_the_configured_azure_deployment_reads_the_sample_invoices():
    """The real check, as an operator runs it, against the deployment the environment or .env names.

    Sends the synthetic samples only and costs a few thousand tokens. Skipped until the endpoint, key and
    deployment are configured; then a failure prints the check's report, which names what to change."""
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=900, cwd=ROOT)
    report = result.stdout + result.stderr
    if result.returncode == 2:
        pytest.skip("Azure AI Foundry is not configured yet: "
                    + next((line.strip() for line in report.splitlines() if " missing " in line), report.strip()))
    print(report)
    assert result.returncode == 0, report
