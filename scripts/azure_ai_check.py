"""Check the Azure AI Foundry connection Invoice Studio reads invoices with, before it goes live.

    .venv/bin/python scripts/azure_ai_check.py            macOS / Linux / Cloud Shell
    .venv\\Scripts\\python scripts\\azure_ai_check.py       Windows
    .venv/bin/pytest -m azure_live                        the same check as a test

It reads the configuration the app reads: the environment, then .env the way scripts/run.sh loads it (a
variable already set wins; --env-file names another file):

    INV_STUDIO_AZURE_ENDPOINT     https://<resource>.openai.azure.com, or .services.ai.azure.com, .cognitiveservices.azure.com
    INV_STUDIO_AZURE_API_KEY      the resource key; not needed with INV_STUDIO_AZURE_AUTH=managed_identity
    INV_STUDIO_AZURE_DEPLOYMENT   the deployment name the app sends as the model (or --deployment)

Then it runs five steps through the app's own code (app.azure_foundry, and Providers.extract, the call every
job makes), so a pass means the app reads with this configuration, not that some other client could:

1. configuration  the endpoint passes the app's allow-list, a key (or managed identity) and a deployment are set
2. connection     one tiny Responses request reaches the deployment and is accepted (a few tokens)
3. read: text     the synthetic sample invoice as text comes back as invoice DEMO-2026-001
4. read: PDF      samples/invoice.pdf with no text beside it: the file input a scanned PDF is sent as
5. read: image    samples/invoice-scan.png: the image input a photo or scan is sent as

--text-only stops after step 3. Only the synthetic samples are sent, never a real invoice; the five steps cost a
few thousand tokens. The key is never printed, saved or logged. Nothing is written to .data: the check uses a
throw-away credential store, so the connection it tests is the one the environment supplies.

The exit code is 0 when every step passed, 1 when one failed (the line says why and what to change), and 2 when
there is nothing to check yet: the endpoint, key or deployment is not configured.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import azure_foundry  # noqa: E402
from app.models import ProcessingOptions  # noqa: E402
from app.providers import Providers, refuse_provider  # noqa: E402
from app.store import Store  # noqa: E402

SAMPLE = json.loads((ROOT / "samples" / "invoice.json").read_text(encoding="utf-8"))
# Printed on every sample the check sends (samples/invoice.json, invoice.pdf, invoice-scan.png).
NUMBER = SAMPLE["number"]
FILES = (("read: PDF", ROOT / "samples" / "invoice.pdf"), ("read: image", ROOT / "samples" / "invoice-scan.png"))
KEY_NAME = re.compile(r"[A-Z0-9_]+")
ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# Azure API Management, which an AI gateway such as Citadel runs on, serves *.azure-api.net.
GATEWAY_SUFFIX = ".azure-api.net"

OK, FAILED, SKIPPED, NOT_CONFIGURED = "ok", "FAILED", "skipped", "missing"

STATUS_HINTS = {
    401: "the key was refused: copy KEY 1 or KEY 2 again from the resource's Keys and Endpoint page, and check the "
         "endpoint is that same resource",
    403: "the resource refused this caller: with managed identity, grant the app identity 'Cognitive Services OpenAI "
         "User' on the resource; with a key, the resource's networking may only accept its private endpoint",
    404: "no such deployment on this resource, or its region does not serve the Responses API (/openai/v1/responses): "
         "check the deployment name in Foundry > Deployments",
    400: "the deployment refused the request: it is not a chat model that serves the Responses API "
         "(an embedding, audio or image deployment, for example)",
    429: "the deployment's rate limit or quota is used up: retry in a minute, or raise its tokens-per-minute quota",
}
# What the app's own error (providers.safe_error and Providers.extract) means for each read step.
READ_HINTS = (
    ("rejected this model, document or output schema", {
        "read: text": "the deployment does not support structured outputs (a strict JSON schema): choose one that does",
        "read: PDF": "the deployment or its region does not accept a PDF as a file input: scanned PDFs will not read, "
                     "choose a vision model in a region that does",
        "read: image": "the deployment does not accept images: choose a vision model",
    }),
    ("rejected the credential", "the key or token was refused during the read"),
    ("rate limit", STATUS_HINTS[429]),
    ("output was incomplete", "the answer stopped before the end: a content filter or the 12000 output-token cap"),
    ("invalid invoice structure", "the answer did not follow the invoice schema: choose a model with structured outputs"),
)


def load_env_file(path):
    """Set the variables a .env file names, the way scripts/run.sh does. Returns the names it set.

    A variable already set in the environment wins, an empty value is skipped and surrounding quotes are
    stripped, so the check sees exactly what ./scripts/run.sh would start the app with."""
    loaded = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\r")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").split(" ")[0]
        if not KEY_NAME.fullmatch(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value and not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded


def sample_text():
    """The synthetic sample invoice (samples/invoice.json) as the text a digital invoice would carry."""
    s = SAMPLE
    lines = [f"{s['supplier_name']}", "TAX INVOICE", f"Invoice No: {s['number']}",
             f"Invoice Date: {s['date']}", f"PO Number: {s['po']}", f"Currency: {s['currency']}",
             "SKU | Barcode | Description | UOM | Qty | Unit price"]
    lines += [f"{x['sku']} | {x['gtin']} | {x['description']} | {x['uom']} | {x['qty']} | {x['price']}"
              for x in s["lines"]]
    lines += [f"Net total: {s['net']}", f"VAT: {s['tax']}"]
    return "\n".join(lines)


def network_reason(error):
    """Name a transport failure and the setting to look at. httpx messages carry no header, so no key."""
    text = str(error)
    if isinstance(error, httpx.ConnectTimeout):
        why = ("no answer to the connection: outbound 443 to the endpoint is blocked (the allow-out-ai NSG rule or "
               "the hub firewall) or the private endpoint is not routed")
    elif isinstance(error, httpx.ProxyError):
        why = "the proxy refused the connection: check HTTPS_PROXY"
    elif "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
        why = ("the TLS certificate was not trusted: behind TLS inspection, set SSL_CERT_FILE to the "
               "organisation's root certificate bundle")
    elif any(s in text for s in ("Name or service not known", "getaddrinfo", "nodename nor servname",
                                 "Temporary failure in name resolution", "No address associated")):
        why = ("the host name does not resolve: check the resource name, or that the private DNS zone "
               "(privatelink.openai.azure.com) is linked to this network")
    elif isinstance(error, httpx.TimeoutException):
        why = "the deployment did not answer in time: retry, and check the resource's health in the portal"
    elif isinstance(error, httpx.ConnectError):
        why = "the connection was refused or reset on the way to the endpoint"
    else:
        why = "the request failed on the network"
    return f"{why} ({type(error).__name__})"


def azure_error_code(response):
    """Azure's own error code (DeploymentNotFound, for example), never its message: only a short identifier."""
    try:
        code = response.json().get("error", {}).get("code")
    except Exception:
        return ""
    return code if isinstance(code, str) and ERROR_CODE.fullmatch(code) else ""


def read_hint(label, message):
    for words, hint in READ_HINTS:
        if words in message:
            return hint[label] if isinstance(hint, dict) else hint
    return ""


class Check:
    def __init__(self):
        self.api_key = ""
        self.results = []

    def report(self, label, status, detail):
        if self.api_key and len(self.api_key) >= 4:
            detail = detail.replace(self.api_key, "[key]")
        self.results.append((label, status))
        print(f"  {label:<14} {status:<7} {detail}".rstrip(), flush=True)
        return status == OK

    def configuration(self, deployment):
        """Step 1: what the app connects with. Returns the deployment to read with, or None."""
        endpoint = os.environ.get(azure_foundry.ENV_ENDPOINT, "").strip()
        managed = azure_foundry.managed_identity()
        self.api_key = os.environ.get(azure_foundry.ENV_API_KEY, "").strip()
        missing = [name for name, value in ((azure_foundry.ENV_ENDPOINT, endpoint),
                                            (azure_foundry.ENV_API_KEY, self.api_key or managed),
                                            (azure_foundry.ENV_DEPLOYMENT, deployment)) if not value]
        if missing:
            self.report("configuration", NOT_CONFIGURED, "set " + ", ".join(missing)
                        + " (in the environment, .env or --env-file; the deployment also by --deployment)")
            return None
        try:
            origin = azure_foundry.normalise_endpoint(endpoint)
        except ValueError as error:
            host = (urlparse(endpoint if "://" in endpoint else "https://" + endpoint).hostname or "").casefold()
            gateway = (f" {host} is an Azure API Management gateway (Citadel runs on one): the app accepts only the "
                       "Foundry resource hosts, so connecting through the gateway needs an app change first."
                       if host.endswith(GATEWAY_SUFFIX) else "")
            self.report("configuration", FAILED, f"{azure_foundry.ENV_ENDPOINT} is refused: {error}.{gateway} While "
                        "it is set the app's pages fail to load (/api/state answers 400): unset it or correct it")
            return None
        try:
            ProcessingOptions(engine="ai", provider="azure", model=deployment)
            sendable = bool(azure_foundry._DEPLOYMENT.fullmatch(deployment))
        except ValidationError:
            sendable = False
        if not sendable:
            self.report("configuration", FAILED, f"the deployment name {deployment!r} is not one the app can send: "
                        "use the name shown in Foundry > Deployments")
            return None
        try:
            refuse_provider("azure")
        except PermissionError as error:
            self.report("configuration", FAILED, f"{error}: add azure to INV_STUDIO_AI_PROVIDERS")
            return None
        auth = "managed identity" if managed else "api-key"
        self.report("configuration", OK, f"endpoint {origin}, deployment {deployment}, auth {auth}")
        return deployment

    def connection(self, providers, deployment):
        """Step 2: one tiny Responses request with the app's URL and auth header, to name what refuses it."""
        try:
            url = providers.responses_url("azure")
            headers = providers.headers("azure")  # the api-key, or a managed identity token: what every read sends
        except ValueError as error:
            return self.report("connection", FAILED, str(error))
        body = {"model": deployment, "input": "Reply with the word OK.", "store": False}
        started = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False) as http:
                response = http.post(url, headers=headers, json=body)
        except httpx.HTTPError as error:
            return self.report("connection", FAILED, network_reason(error))
        took = time.monotonic() - started
        if response.status_code == 200:
            return self.report("connection", OK, f"the deployment answered ({took:.1f} s)")
        status = response.status_code
        code = azure_error_code(response)
        code = "" if code == str(status) else code  # Azure's 401 body names its code "401"
        hint = STATUS_HINTS.get(status) or ("Azure answered with a server error: retry, then check the resource's "
                                            "health in the portal" if status >= 500 else "unexpected answer")
        if status in (301, 302, 303, 307, 308):
            hint = "the endpoint redirected, which the app never follows: use the resource endpoint itself"
        return self.report("connection", FAILED, f"HTTP {status}{' ' + code if code else ''}: {hint}")

    def read(self, providers, label, path, text, deployment):
        """Steps 3 to 5: the read a job makes (Providers.extract), checked against the sample's invoice number."""
        options = ProcessingOptions(engine="ai", ai_fallback=True, provider="azure", model=deployment)
        started = time.monotonic()
        try:
            invoice, usage = providers.extract(path, text, options, examples="")
        except ValueError as error:
            hint = read_hint(label, str(error))
            return self.report(label, FAILED, str(error) + (f" ({hint})" if hint else ""))
        except httpx.HTTPError as error:
            return self.report(label, FAILED, network_reason(error))
        took = time.monotonic() - started
        tokens = ""
        if isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int):
            tokens = f", {usage['input_tokens']} in / {usage['output_tokens']} out tokens"
        if invoice.number != NUMBER:
            return self.report(label, FAILED, f"the deployment answered, but read invoice number {invoice.number!r} "
                               f"instead of {NUMBER}: it did not read the document; choose a vision model")
        return self.report(label, OK, f"invoice {invoice.number} read, {len(invoice.lines)} lines "
                                      f"({took:.1f} s{tokens})")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check the Azure AI Foundry connection Invoice Studio reads with.")
    parser.add_argument("--env-file", help="read settings from this file (default: the repository's .env, if any)")
    parser.add_argument("--deployment", help="the deployment to check (default: INV_STUDIO_AZURE_DEPLOYMENT)")
    parser.add_argument("--text-only", action="store_true", help="stop after the text read: no PDF or image")
    args = parser.parse_args(argv)

    env_file = Path(args.env_file) if args.env_file else ROOT / ".env"
    if args.env_file and not env_file.is_file():
        print(f"No such file: {env_file}")
        return 2
    loaded = load_env_file(env_file) if env_file.is_file() else []
    print("Azure AI Foundry connection check" + (f" (settings from the environment and {env_file.name})"
                                                  if loaded else " (settings from the environment)"), flush=True)
    check = Check()
    deployment = (args.deployment or os.environ.get(azure_foundry.ENV_DEPLOYMENT, "")).strip()
    steps = ["connection", "read: text"] + ([] if args.text_only else [label for label, _ in FILES])
    with tempfile.TemporaryDirectory(prefix="inv-azure-check-") as scratch:
        work = Path(scratch)
        if check.configuration(deployment) is None:
            for label in steps:
                check.report(label, SKIPPED, "")
        else:
            providers = Providers(Store(work / "store"), chatgpt=None)
            ready = check.connection(providers, deployment)
            text_file = work / "invoice.txt"
            text_file.write_text(sample_text(), encoding="utf-8")
            reads = [("read: text", text_file, sample_text())]
            if not args.text_only:
                reads += [(label, path, "") for label, path in FILES]
            for label, path, text in reads:
                if ready:
                    check.read(providers, label, path, text, deployment)
                else:
                    check.report(label, SKIPPED, "")

    statuses = [status for _, status in check.results]
    if NOT_CONFIGURED in statuses:
        print("Nothing to check yet: Azure AI Foundry is not configured.")
        return 2
    if all(status == OK for status in statuses):
        print("Every step passed: Invoice Studio can read invoices with this Azure AI Foundry deployment.")
        return 0
    print("Not working yet: fix the first FAILED line and run the check again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
