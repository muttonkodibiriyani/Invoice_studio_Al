import base64
import io
import mimetypes
import os
import re
import time
from pathlib import Path

import httpx

from .models import IMAGE_FORMATS, ai_schema, parse_ai_output


DEFAULT_MODEL = "gemini-3.7-flash"
DEFAULT_LOCATION = "global"
ADC_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
MAX_INLINE_BYTES = 12_000_000
MAX_RESPONSE_BYTES = 5_000_000
MAX_OCR_CHARS = 100_000
# The model's output cap. A long invoice (hundreds of lines) overruns a smaller budget
# and returns a truncated candidate, which is not a usable invoice.
MAX_OUTPUT_TOKENS = 65_535
# Pages per request when a whole document did not come back complete.
PAGE_CHUNK = 4
INCOMPLETE = "Vertex AI did not return a complete valid invoice. Review the document manually."
TIMED_OUT = "Vertex AI request timed out. Retry or review manually."
CHUNK_INSTRUCTION = (
    "Extract this invoice. Only part of the document is supplied: read every line printed "
    "on these pages and no others, and give each line the page number printed on its page."
)
# A burst of uploads can hit the per-minute quota: one retry after a short pause, then the local readers.
RETRY_AFTER_SECONDS = 12
MAX_RETRY_AFTER_SECONDS = 30
_sleep = time.sleep

_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_LOCATION = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_MODEL = re.compile(r"^gemini-[a-z0-9][a-z0-9._-]{0,99}$")


def _adc_access_token():
    try:
        import google.auth
        from google.auth.transport.requests import Request

        credentials, _ = google.auth.default(scopes=[ADC_SCOPE])
        credentials.refresh(Request())
        if not credentials.token:
            raise RuntimeError
        return credentials.token
    except Exception:
        raise ValueError(
            "Vertex AI could not obtain Google Cloud application credentials. "
            "Check the service identity for this workspace."
        ) from None


def _vertex_schema(node):
    """Convert the shared strict JSON schema to Vertex's responseSchema shape."""
    kind = node.get("type")
    nullable = isinstance(kind, list) and "null" in kind
    if isinstance(kind, list):
        kind = next((value for value in kind if value != "null"), None)
    types = {
        "object": "OBJECT",
        "array": "ARRAY",
        "string": "STRING",
        "integer": "INTEGER",
        "number": "NUMBER",
        "boolean": "BOOLEAN",
    }
    result = {}
    if kind in types:
        result["type"] = types[kind]
    if nullable:
        result["nullable"] = True
    if "properties" in node:
        result["properties"] = {
            name: _vertex_schema(value) for name, value in node["properties"].items()
        }
        result["propertyOrdering"] = list(node["properties"])
    if "items" in node:
        result["items"] = _vertex_schema(node["items"])
    if "required" in node:
        result["required"] = list(node["required"])
    return result


def _document_parts(path):
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if mime == "application/pdf":
        raw = path.read_bytes()
        if len(raw) > MAX_INLINE_BYTES:
            raise ValueError("The document is too large for inline Vertex AI extraction")
        return [{"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode()}}]
    if mime.startswith("image/"):
        from PIL import Image, ImageOps, ImageSequence

        parts = []
        total = 0
        with Image.open(path, formats=IMAGE_FORMATS) as source:
            for number, frame in enumerate(ImageSequence.Iterator(source)):
                if number >= 20:
                    raise ValueError("AI image input exceeds 20 frames")
                image = ImageOps.exif_transpose(frame).convert("RGB")
                image.thumbnail((2600, 2600))
                encoded = io.BytesIO()
                image.save(encoded, format="PNG")
                raw = encoded.getvalue()
                total += len(raw)
                if total > MAX_INLINE_BYTES:
                    raise ValueError("The document is too large for inline Vertex AI extraction")
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": base64.b64encode(raw).decode(),
                        }
                    }
                )
        return parts
    return []


def _configuration(model):
    project = os.environ.get("VERTEX_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    location = os.environ.get("VERTEX_LOCATION", DEFAULT_LOCATION)
    selected_model = model or os.environ.get("VERTEX_MODEL", DEFAULT_MODEL)
    if not _PROJECT_ID.fullmatch(project):
        raise ValueError("Set VERTEX_PROJECT_ID to the Google Cloud project that owns Vertex AI")
    if not _LOCATION.fullmatch(location):
        raise ValueError("VERTEX_LOCATION is not a valid Google Cloud location")
    if not _MODEL.fullmatch(selected_model):
        raise ValueError("The selected Vertex AI model name is not valid")
    return project, location, selected_model


def _endpoint(project, location, model):
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return (
        f"https://{host}/v1/projects/{project}/locations/{location}/"
        f"publishers/google/models/{model}:generateContent"
    )


def _retry_after(response):
    """Seconds to wait before the one retry: the server's Retry-After when sane, else a fixed pause."""
    try:
        value = int(float((getattr(response, "headers", None) or {}).get("Retry-After", "")))
    except (TypeError, ValueError):
        return RETRY_AFTER_SECONDS
    return max(1, min(value, MAX_RETRY_AFTER_SECONDS))


def _provider_error(status):
    messages = {
        400: "Vertex AI rejected this model, document or output schema.",
        401: "Vertex AI rejected the Google Cloud service identity.",
        403: "The Google Cloud service identity cannot use the selected Vertex AI model.",
        404: "The selected Vertex AI model is not available in this location.",
        429: "Vertex AI usage or quota limit reached. Retry later.",
    }
    return messages.get(status, f"Vertex AI request failed (HTTP {status}). No invoice was approved.")


def _pdf_pages(path: Path) -> int:
    """The document's page count, or 0 when it is not a PDF this build can split."""
    if (mimetypes.guess_type(path.name)[0] or "") != "application/pdf":
        return 0
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(path)
        try:
            return len(document)
        finally:
            document.close()
    except Exception:
        return 0


def _page_chunk(path: Path, first: int, last: int) -> bytes:
    """A new PDF holding pages [first, last) of the document, in their printed order."""
    import pypdfium2 as pdfium

    source = pdfium.PdfDocument(path)
    try:
        chunk = pdfium.PdfDocument.new()
        chunk.import_pages(source, list(range(first, last)))
        buffer = io.BytesIO()
        chunk.save(buffer)
        return buffer.getvalue()
    finally:
        source.close()


def _sum_lines(lines, field):
    from decimal import Decimal, InvalidOperation

    values = [getattr(line, field) for line in lines]
    if not values or any(value is None for value in values):
        return None
    try:
        return sum((Decimal(str(value)) for value in values), Decimal(0))
    except (InvalidOperation, ValueError):
        return None


def _merge_chunks(results):
    """Join the per-page reads of one document: disjoint pages, so only headers need deciding.

    A line keeps the page it was printed on. A header value is taken when the chunks agree;
    a total is taken when it is the sum of the merged lines, which is what proves the whole
    document was read. Anything else is left empty for review rather than guessed.
    """
    from decimal import Decimal, InvalidOperation

    from .models import Invoice

    lines = []
    for first, invoice, _meta in results:
        for line in invoice.lines:
            page = line.page
            lines.append(
                line.model_copy(update={"page": min(20, first + page)}) if page else line
            )
    header = {}
    for field in Invoice.model_fields:
        if field == "lines":
            continue
        values = [
            getattr(invoice, field) for _first, invoice, _meta in results
            if getattr(invoice, field) not in (None, "")
        ]
        if not values:
            continue
        if field in ("net", "tax"):
            printed = _sum_lines(lines, "net_amount" if field == "net" else "tax_amount")
            proven = None
            for value in values:
                try:
                    if printed is not None and Decimal(str(value)) == printed:
                        proven = value
                        break
                except (InvalidOperation, ValueError):
                    continue
            if proven is not None:
                header[field] = proven
            elif len({str(value) for value in values}) == 1:
                header[field] = values[0]
            continue
        if len({str(value) for value in values}) == 1:
            header[field] = values[0]
    invoice = Invoice.model_validate({**header, "lines": [line.model_dump() for line in lines]})
    meta = {"chunks": len(results)}
    evidence = {}
    for _first, _invoice, chunk_meta in results:
        for key, value in (chunk_meta or {}).items():
            if key == "evidence":
                for field, entry in (value or {}).items():
                    evidence.setdefault(field, entry)
            elif isinstance(value, int):
                meta[key] = meta.get(key, 0) + value
    if evidence:
        meta["evidence"] = evidence
    return invoice, meta


class VertexProvider:
    def models(self):
        selected = os.environ.get("VERTEX_MODEL", DEFAULT_MODEL)
        return [{"id": selected, "name": selected}]

    def extract(self, path: Path, text: str, model: str, prompt: str):
        project, location, selected_model = _configuration(model)
        parts = _document_parts(path)
        if not parts and not text.strip():
            raise ValueError("No readable text or supported document image is available for Vertex AI")
        request = list(parts) + [
            {
                "text": "Extract this invoice. Document text, if available:\n"
                + text[:MAX_OCR_CHARS]
            }
        ]
        attempts = 0
        while True:
            attempts += 1
            try:
                return self._generate(project, location, selected_model, prompt, request)
            except ValueError as error:
                reason = str(error)
                if reason not in (INCOMPLETE, TIMED_OUT):
                    raise
                # A read comes back cut off now and then and whole on the next attempt, so the
                # whole document is asked for twice before it is taken apart.
                if reason == TIMED_OUT or attempts > 1:
                    break
        pages = _pdf_pages(path)
        if pages < 2:
            raise ValueError(reason)
        # An invoice that did not come back whole is read a few pages at a time; a short one is
        # split in half, so a two-page invoice is still read a page each.
        step = max(1, min(PAGE_CHUNK, pages // 2))
        return self._generate_by_pages(project, location, selected_model, prompt, path, pages, step)

    def _generate_by_pages(self, project, location, model, prompt, path, pages, step=PAGE_CHUNK):
        results = []
        for first in range(0, pages, step):
            chunk = _page_chunk(path, first, min(first + step, pages))
            if len(chunk) > MAX_INLINE_BYTES:
                raise ValueError("The document is too large for inline Vertex AI extraction")
            parts = [
                {
                    "inlineData": {
                        "mimeType": "application/pdf",
                        "data": base64.b64encode(chunk).decode(),
                    }
                },
                {"text": CHUNK_INSTRUCTION},
            ]
            results.append(
                (first, *self._generate(project, location, model, prompt, parts))
            )
        return _merge_chunks(results)

    def _generate(self, project, location, selected_model, prompt, parts):
        body = {
            "systemInstruction": {"parts": [{"text": prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _vertex_schema(ai_schema()),
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
                "thinkingConfig": {"thinkingLevel": "LOW"},
            },
        }
        headers = {
            "Authorization": "Bearer " + _adc_access_token(),
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(
                timeout=httpx.Timeout(240, connect=15), follow_redirects=False
            ) as http:
                response = http.post(
                    _endpoint(project, location, selected_model), headers=headers, json=body
                )
                if response.status_code == 429:
                    _sleep(_retry_after(response))
                    response = http.post(
                        _endpoint(project, location, selected_model), headers=headers, json=body
                    )
        except httpx.TimeoutException:
            raise ValueError(TIMED_OUT) from None
        except httpx.RequestError:
            raise ValueError("Vertex AI could not be reached. Retry or review manually.") from None
        if response.status_code != 200:
            raise ValueError(_provider_error(response.status_code))
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ValueError("Vertex AI response exceeded the size limit")
        try:
            result = response.json()
            candidates = result.get("candidates") or []
            candidate = candidates[0]
            if candidate.get("finishReason") != "STOP":
                raise ValueError
            output = "".join(
                part.get("text", "")
                for part in candidate.get("content", {}).get("parts", [])
                if not part.get("thought")
            )
            if not output or len(output.encode()) > MAX_RESPONSE_BYTES:
                raise ValueError
            invoice, evidence = parse_ai_output(output)
        except Exception:
            raise ValueError(INCOMPLETE) from None
        return invoice, {
            **result.get("usageMetadata", {}),
            **({"evidence": evidence} if evidence else {}),
        }
