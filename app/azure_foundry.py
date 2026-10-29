"""Azure AI Foundry connection: an operator-supplied resource endpoint and API key.

Foundry serves the OpenAI v1 data plane, so the invoice request body is the one
the OpenAI adapter already builds; only the base URL and the auth header differ.
The endpoint is typed in by an operator and then fetched by the server, so it is
normalised and restricted to Azure resource hosts before anything is saved.

The connection lives in the encrypted credential store (Fernet over SQLite locally,
over Postgres in the cloud), beside the OpenAI and Anthropic keys, and is
workspace-wide: an administrator connects it, every member's reads use it. The
environment seeds a deployment that is provisioned without the UI, with the key
mounted from Secret Manager the way the hosted service mounts its other secrets.
"""
import os
import re
from urllib.parse import urlparse

SECRET = "azure"
ENV_ENDPOINT = "INV_STUDIO_AZURE_ENDPOINT"
ENV_API_KEY = "INV_STUDIO_AZURE_API_KEY"
# The deployment the hosted workspace reads with when nobody has chosen a model: the
# managed default, the way VERTEX_MODEL is for Vertex.
ENV_DEPLOYMENT = "INV_STUDIO_AZURE_DEPLOYMENT"

# Azure resource hosts that serve the Foundry/Azure OpenAI v1 data plane. The
# server issues the request, so an endpoint outside these suffixes is refused:
# it must never be able to reach a private address or an unrelated service.
ALLOWED_SUFFIXES = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")
MAX_ENDPOINT = 300
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_DEPLOYMENT = re.compile(r"^[A-Za-z0-9._:/-]{1,150}$")
_REFUSED = ("Enter the Azure resource endpoint, for example "
            "https://your-resource.openai.azure.com or https://your-resource.services.ai.azure.com")

# Deployments that cannot read an invoice. Azure deployment names are operator
# chosen, so the list only drops families that are never vision/text models.
NON_TEXT = ("embedding", "whisper", "tts", "dall-e", "sora", "transcrib", "speech", "audio", "realtime")


def normalise_endpoint(value):
    """Return the bare https origin of an Azure resource, or refuse the value."""
    if not isinstance(value, str):
        raise ValueError(_REFUSED)
    text = value.strip().rstrip("/")
    if not text or len(text) > MAX_ENDPOINT:
        raise ValueError(_REFUSED)
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(_REFUSED)
    # A resource path such as /openai/v1 is accepted and dropped; the adapter adds its own.
    if parsed.path.strip("/") not in ("", "openai", "openai/v1"):
        raise ValueError(_REFUSED)
    try:
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        raise ValueError(_REFUSED) from None
    if port not in (None, 443) or not _HOSTNAME.fullmatch(host):
        raise ValueError(_REFUSED)
    if not host.endswith(ALLOWED_SUFFIXES):
        raise ValueError("Only an Azure resource endpoint is accepted: a host ending in "
                         + ", ".join(ALLOWED_SUFFIXES))
    # Require a resource label in front of the suffix, so the suffix itself is not an endpoint.
    suffix = next(s for s in ALLOWED_SUFFIXES if host.endswith(s))
    if not host[: -len(suffix)]:
        raise ValueError(_REFUSED)
    return "https://" + host


def base_url(origin):
    """The OpenAI v1 data-plane base for a normalised resource origin."""
    return origin + "/openai/v1"


def env_connection():
    """An endpoint/key pair supplied by deployment configuration, if both are set."""
    endpoint = os.environ.get(ENV_ENDPOINT, "").strip()
    key = os.environ.get(ENV_API_KEY, "").strip()
    if not endpoint or not key:
        return None
    return {"endpoint": normalise_endpoint(endpoint), "api_key": key}


def env_deployment():
    """The deployment name the environment names as the managed default, or ""."""
    name = os.environ.get(ENV_DEPLOYMENT, "").strip()
    return name if _DEPLOYMENT.fullmatch(name) else ""


def _saved(store, c=None):
    saved = store.secret(SECRET, c=c)
    if isinstance(saved, dict) and saved.get("endpoint") and saved.get("api_key"):
        return saved
    return None


class AzureFoundry:
    """Store one Azure Foundry endpoint and key, and never return the key to a caller."""

    def __init__(self, store):
        self.store = store

    def connect(self, endpoint, api_key):
        key = api_key.strip() if isinstance(api_key, str) else ""
        if not key:
            raise ValueError("Enter the Azure resource API key")
        origin = normalise_endpoint(endpoint)
        self.store.secret(SECRET, {"version": 1, "endpoint": origin, "api_key": key})
        return {"connected": True, "provider": "azure", "endpoint": origin}

    def configured(self, c=None):
        return _saved(self.store, c) is not None or env_connection() is not None

    def endpoint(self, c=None):
        """The connected endpoint for display. The key is never part of this."""
        saved = _saved(self.store, c)
        if saved:
            return saved["endpoint"]
        supplied = env_connection()
        return supplied["endpoint"] if supplied else ""

    def managed_default(self, c=None):
        """The deployment the environment pre-selects, when the environment also connects Azure."""
        return env_deployment() if env_connection() is not None else ""

    def credentials(self):
        """The (base URL, key) the adapter calls with. A saved connection wins over the environment."""
        saved = _saved(self.store) or env_connection()
        if not saved:
            raise ValueError("Connect Azure AI Foundry with its endpoint and API key first")
        return base_url(saved["endpoint"]), saved["api_key"]

    def disconnect(self):
        self.store.secret(SECRET, delete=True)
        return {"connected": self.configured(), "provider": "azure"}
