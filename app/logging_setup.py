"""Structured JSON logging for the cloud workspace.

Every line is one JSON object (Cloud Logging parses `severity` and `message`). A request id travels in a
context variable so handler and startup logs can be correlated. Nothing here ever logs tokens, out-of-band
codes, links or secrets: `redact` scrubs the usual shapes from free text before it is written.
"""
import json
import logging
import os
import re
import sys
import traceback
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

request_id = ContextVar("invoice_request_id", default="")

_SEVERITY = {"DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL"}
# A field whose name holds one of these words carries a credential: api-key, Authorization,
# Ocp-Apim-Subscription-Key, client_secret, access_token, password and the like.
_SECRET_NAME = r"[\w-]{0,40}(?:key|secret|token|password|authorization)[\w-]{0,40}"  # bounded: no quadratic backtracking
_SECRET_FIELD = re.compile(r"(?i)^" + _SECRET_NAME + r"$")
_REDACTIONS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # Dict, JSON and header-object shapes: {'api-key': 'v'}, {"client_secret": "v"}, Headers({'authorization': 'v'}).
    # The name is kept so the line still says what was there; a quoted value is masked whole, escapes included.
    re.compile(r"""(?i)((["'])""" + _SECRET_NAME + r"""\2\s*:\s*)(?:b?"(?:[^"\\]|\\.)*"|b?'(?:[^'\\]|\\.)*'|[^\s,}\]]+)"""),
    # An HTTP header line: "api-key: v", "Ocp-Apim-Subscription-Key: v".
    re.compile(r"(?im)^(\s*" + _SECRET_NAME + r"\s*:\s*)(?!\[redacted\])\S.*$"),
    re.compile(r"(?i)\b((?:oobCode|oob_code|" + _SECRET_NAME + r")=)[^&\s\"']+"),
    re.compile(r"https?://[^\s\"'<>]+"),
)


def redact(text):
    """Remove bearer tokens, JWT-shaped strings, credential-named fields (query, dict, JSON or header shape) and whole URLs."""
    if not isinstance(text, str):
        text = str(text)
    for pattern in _REDACTIONS:
        text = pattern.sub(lambda m: (m.group(1) if pattern.groups else "") + "[redacted]", text)
    return text


def redact_value(value, name=""):
    """Redact a structured log field: a credential-named field is masked whole, containers are walked."""
    if name and _SECRET_FIELD.match(str(name)) and value is not None:
        return "[redacted]"
    if isinstance(value, dict):
        return {k: redact_value(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    return redact(value) if isinstance(value, (str, bytes)) else value


def new_request_id(header_value=None):
    """A Cloud Trace id when the front end supplies one, otherwise a random id."""
    if header_value:
        candidate = header_value.split("/", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z0-9-]{8,64}", candidate):
            return candidate
    return uuid.uuid4().hex


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "severity": _SEVERITY.get(record.levelname, "DEFAULT"),
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        rid = request_id.get()
        if rid:
            payload["request_id"] = rid
        fields = getattr(record, "studio", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                payload[key] = redact_value(value, key)
        if record.exc_info:
            payload["exception"] = redact("".join(traceback.format_exception(*record.exc_info)))[-8000:]
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure(force=None):
    """Install the JSON formatter on the root and uvicorn loggers. Enabled in cloud mode or with INV_STUDIO_JSON_LOGS=1."""
    enabled = force if force is not None else (os.getenv("INV_STUDIO_CLOUD") == "1" or os.getenv("INV_STUDIO_JSON_LOGS") == "1")
    if not enabled:
        return False
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if name == "uvicorn.access" and not logger.handlers and not logger.propagate:
            # Started with --no-access-log (the image does): uvicorn writes access lines whenever this logger
            # reaches a handler, so propagating would bring them back next to the app's own request line.
            continue
        logger.handlers[:] = []
        logger.propagate = True
    return True


def event(logger, message, **fields):
    """Log one structured line: `event(log, "request", status=200, latency_ms=12)`."""
    logger.info(message, extra={"studio": fields})
