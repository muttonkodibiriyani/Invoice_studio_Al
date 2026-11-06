"""Azure AI Foundry connection: an operator-supplied endpoint, a name to call, and a key or managed identity.

Two routes. A resource endpoint and a model deployment: Foundry serves the OpenAI v1 data plane, so the invoice request
body is the one the OpenAI adapter already builds; only the base URL and the auth header differ. A project endpoint
(/api/projects/<project>) and an agent: POST <project>/openai/v1/responses with an agent_reference (Microsoft Learn,
"Build with runtime components in Foundry Agent Service"), where the agent supplies the model. Nothing is saved until a
real call to the deployment or agent answered (Providers.test_azure).
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
# key (default): the api-key header. managed_identity: an Entra access token for the app's own managed
# identity, so no AI key exists anywhere (InfoSec LLD §9). The identity needs the Cognitive Services OpenAI
# User role on the Foundry resource; a gateway in front (Citadel) may name its own scope.
ENV_AUTH = "INV_STUDIO_AZURE_AUTH"
ENV_SCOPE = "INV_STUDIO_AZURE_TOKEN_SCOPE"
MANAGED_IDENTITY = "managed_identity"
DEFAULT_SCOPE = "https://cognitiveservices.azure.com/.default"
AGENT_SCOPE = "https://ai.azure.com/.default"  # the project route's scope, Microsoft Learn "Foundry SDKs and endpoints"
_SCOPE = re.compile(r"^(https://|api://)[A-Za-z0-9._:/-]{1,200}/\.default$")
# One AI gateway (Citadel) by exact host, named by whoever deploys (deploy kit CITADEL_GATEWAY_HOST), never typed in the
# app. It is accepted beside the Azure resource hosts; no other host under its domain is.
ENV_GATEWAY = "INV_STUDIO_AZURE_GATEWAY_HOST"

# Azure resource hosts that serve the Foundry/Azure OpenAI v1 data plane. The
# server issues the request, so an endpoint outside these suffixes is refused:
# it must never be able to reach a private address or an unrelated service.
ALLOWED_SUFFIXES = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")
MAX_ENDPOINT = 300
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_DEPLOYMENT = re.compile(r"^[A-Za-z0-9._:/-]{1,150}$")
_PROJECT = re.compile(r"^api/projects/([A-Za-z0-9_][A-Za-z0-9._-]{0,63})(?:/openai(?:/v1)?)?$")
PROJECT_SUFFIX = ".services.ai.azure.com"  # the one host family that serves a project
_REFUSED = ("Enter the Azure endpoint, for example https://your-resource.openai.azure.com, or a Foundry project: "
            "https://your-resource.services.ai.azure.com/api/projects/your-project")

# Deployments that cannot read an invoice. Azure deployment names are operator
# chosen, so the list only drops families that are never vision/text models.
NON_TEXT = ("embedding", "whisper", "tts", "dall-e", "sora", "transcrib", "speech", "audio", "realtime")


def valid_gateway(host):
    """A bare DNS name. An address, a port or a path is no gateway."""
    return bool(_HOSTNAME.fullmatch(host)) and not host.replace(".", "").isdigit()


def gateway_host():
    """The one gateway host this deployment names, or ""."""
    host = os.environ.get(ENV_GATEWAY, "").strip().casefold()
    return host if valid_gateway(host) else ""


def normalise_endpoint(value, gateway=None):
    """Return the bare https origin of an Azure resource or the named gateway, or a Foundry project's URL, or refuse.

    gateway is the host to accept, for the pre-deploy check; by default the one this deployment names."""
    gateway = gateway_host() if gateway is None else gateway
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
    project = _PROJECT.fullmatch(parsed.path.strip("/"))
    if not project and parsed.path.strip("/") not in ("", "openai", "openai/v1"):
        raise ValueError(_REFUSED)
    try:
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        raise ValueError(_REFUSED) from None
    if port not in (None, 443) or not _HOSTNAME.fullmatch(host):
        raise ValueError(_REFUSED)
    if project and not host.endswith(PROJECT_SUFFIX):
        raise ValueError(_REFUSED)
    if gateway and host == gateway:
        return "https://" + host
    if not host.endswith(ALLOWED_SUFFIXES):
        raise ValueError("Only an Azure resource endpoint is accepted: a host ending in "
                         + ", ".join(ALLOWED_SUFFIXES))
    # Require a resource label in front of the suffix, so the suffix itself is not an endpoint.
    suffix = next(s for s in ALLOWED_SUFFIXES if host.endswith(s))
    if not host[: -len(suffix)]:
        raise ValueError(_REFUSED)
    return "https://" + host + (f"/api/projects/{project[1]}" if project else "")


def base_url(origin):
    """The OpenAI v1 data-plane base for a normalised resource origin or project."""
    return origin + "/openai/v1"


def is_project(endpoint):
    """A project endpoint: the name it is connected with is an agent, not a deployment."""
    return "/api/projects/" in endpoint


def managed_identity():
    """True when the deployment authenticates to Foundry with its managed identity instead of an api-key."""
    return os.environ.get(ENV_AUTH, "").strip().casefold() == MANAGED_IDENTITY


def token_scope(endpoint=""):
    scope = os.environ.get(ENV_SCOPE, "").strip()
    return scope if _SCOPE.fullmatch(scope) else AGENT_SCOPE if is_project(endpoint) else DEFAULT_SCOPE


_credential = None


def bearer_token(scope=None):
    """An Entra access token for the app's managed identity. azure-identity caches it until near expiry."""
    global _credential
    if _credential is None:
        from azure.identity import DefaultAzureCredential  # the cloud image installs the azure extra
        _credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return _credential.get_token(scope or token_scope()).token


def env_refusal():
    """Why the environment's endpoint is refused, or "". A refused value turns the AI off, never the app."""
    try:
        normalise_endpoint(os.environ.get(ENV_ENDPOINT, "").strip() or "https://unset.openai.azure.com")
    except ValueError as error:
        return f"{ENV_ENDPOINT} is refused: {error}"
    return ""


def env_connection():
    """An endpoint and credential supplied by deployment configuration, if complete and allowed.

    With managed identity the endpoint alone is enough: the key secret, if one is mounted, is never used."""
    endpoint = os.environ.get(ENV_ENDPOINT, "").strip()
    if not endpoint or env_refusal():
        return None
    if managed_identity():
        return {"endpoint": normalise_endpoint(endpoint), "api_key": None, "auth": MANAGED_IDENTITY}
    key = os.environ.get(ENV_API_KEY, "").strip()
    if not key:
        return None
    return {"endpoint": normalise_endpoint(endpoint), "api_key": key}


def env_deployment():
    """The deployment name the environment names as the managed default, or ""."""
    name = os.environ.get(ENV_DEPLOYMENT, "").strip()
    return name if _DEPLOYMENT.fullmatch(name) else ""


def _saved(store, c=None):
    saved = store.secret(SECRET, c=c)
    if isinstance(saved, dict) and saved.get("endpoint") and (saved.get("api_key") or saved.get("auth") == MANAGED_IDENTITY):
        return saved
    return None


def headers_for(endpoint, key):
    if key:
        return {"api-key": key}
    role = "Foundry Agent Consumer role on the project" if is_project(endpoint) else "Cognitive Services OpenAI User role on the resource"
    try:
        return {"Authorization": "Bearer " + bearer_token(token_scope(endpoint))}
    except Exception:
        raise ValueError(f"The app identity could not get a token for Azure AI Foundry. Check that it has the {role}.") from None


class AzureFoundry:
    """Store one Azure Foundry connection, and never return the key to a caller."""

    def __init__(self, store):
        self.store = store

    def prepare(self, endpoint, api_key=None, name="", auth="key"):
        """A connection checked for shape and not saved: Providers.test_azure calls it, save() then keeps it."""
        key = api_key.strip() if isinstance(api_key, str) else ""
        if auth != MANAGED_IDENTITY and not key:
            raise ValueError("Enter the API key, or choose managed identity")
        origin = normalise_endpoint(endpoint)
        name = (name or "").strip() or env_deployment()
        if name and not _DEPLOYMENT.fullmatch(name):
            raise ValueError("The agent or deployment name has characters Azure does not allow")
        if auth == MANAGED_IDENTITY:
            return {"version": 2, "endpoint": origin, "name": name, "api_key": None, "auth": MANAGED_IDENTITY}
        return {"version": 2, "endpoint": origin, "name": name, "api_key": key, "auth": "key"}

    def save(self, connection):
        """Keep a connection that passed its test, replacing the one before it."""
        self.store.secret(SECRET, connection)
        return {"connected": True, "provider": "azure", "endpoint": connection["endpoint"], "name": connection["name"],
                "route": "agent" if is_project(connection["endpoint"]) else "deployment"}

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
        """The deployment or agent the saved connection tested, else the one the environment pre-selects."""
        saved = _saved(self.store, c)
        if saved and saved.get("name"):
            return saved["name"]
        return env_deployment() if env_connection() is not None else ""

    def agent(self):
        """The agent every read goes to, or "" on the deployment route."""
        return self.managed_default() if is_project(self.endpoint()) else ""

    def credentials(self):
        """The (base URL, key) the adapter calls with. A saved connection wins over the environment.

        The key is None when the deployment uses its managed identity: auth_headers() then sends a token."""
        saved = _saved(self.store) or env_connection()
        if not saved:
            raise ValueError(env_refusal() or "Connect Azure AI Foundry with its endpoint and API key first")
        return base_url(saved["endpoint"]), saved["api_key"]

    def auth_headers(self):
        """The header that authenticates one Foundry request: an api-key, or a managed identity bearer token."""
        base, key = self.credentials()
        return headers_for(base.removesuffix("/openai/v1"), key)

    def disconnect(self):
        self.store.secret(SECRET, delete=True)
        return {"connected": self.configured(), "provider": "azure"}
