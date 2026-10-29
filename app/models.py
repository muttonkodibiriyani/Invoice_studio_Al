from decimal import Decimal
from typing import ClassVar, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


# The one list of selectable AI readers. Local and cloud offer exactly this set;
# which entries are connected is configuration, never a different build.
AI_PROVIDERS = ("openai", "anthropic", "azure", "chatgpt", "claude_local", "vertex")
# Connections an operator supplies as a bare API key saved against the provider name.
API_KEY_PROVIDERS = ("openai", "anthropic")
# The only image formats Pillow may decode; every other decoder (FPX, EPS, XBM...) stays unreachable.
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "BMP", "TIFF")
# A model name goes to the claude CLI as the value of --model, so it must not start like an option.
MODEL_NAME = r"^(?:[A-Za-z0-9][A-Za-z0-9._:/-]*)?$"


class _Capped(StrictModel):
    """A model whose free-text fields each carry a maximum length (RULE 8).

    The caps sit well above what a printed invoice holds in that field; a longer value is not
    the field but a read that took in a block of the page. Checks downstream (fine rules,
    matching, the target check, the workbooks, text_key) run patterns over these values, so
    the cap is what bounds their input. A read (local reader, AI, a stored invoice) keeps an
    over-cap field empty, so it shows as a gap to fill or review instead of a wrong value;
    a member edit over a cap is refused with a 422 (the Edited* models below).
    """
    refuse_over_cap: ClassVar[bool] = False

    @model_validator(mode="before")
    @classmethod
    def _drop_over_cap(cls, data):
        if cls.refuse_over_cap or not isinstance(data, dict):
            return data
        over = [name for name, value in data.items() if isinstance(value, str) and name in cls.model_fields
                and len(value.strip()) > _max_length(cls.model_fields[name])]
        return {**data, **dict.fromkeys(over)} if over else data


def _max_length(field):
    return next((m.max_length for m in field.metadata if getattr(m, "max_length", None)), float("inf"))


class Line(_Capped):
    item_id: str | None = Field(default=None, max_length=100)  # an item or article code
    sku: str | None = Field(default=None, max_length=100)  # a supplier code; the target's UPC column holds 100
    gtin: str | None = Field(default=None, max_length=32)  # a GTIN is at most 14 digits
    # The printed non-GTIN code of a part-number column, kept whole as printed even when
    # the description's own code fills sku. Local table reads only; never asked of an AI.
    part_code: str | None = Field(default=None, max_length=100)
    # A long item text wraps over a few printed rows; this is room for several.
    description: str | None = Field(default=None, max_length=1000)
    qty: Decimal | None = None
    uom: str | None = Field(default=None, max_length=32)  # a unit: PCS, CTN, KG, "Box of 12"
    price: Decimal | None = None
    net_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    evidence: str | None = Field(default=None, max_length=500)  # the readers' own quote of the row is cut at 500
    page: int | None = Field(default=None, ge=1, le=20)
    # A complete printed barcode that fails the GTIN check digit, digits only; gtin stays empty.
    # Set by the reader, never by an AI: it is not in the extraction schema.
    barcode_unchecked: str | None = Field(default=None, max_length=32)


class Invoice(_Capped):
    number: str | None = Field(default=None, max_length=100)  # the target's Document column holds 200
    # A registered legal name with its branch or trading name.
    supplier_name: str | None = Field(default=None, max_length=300)
    buyer_name: str | None = Field(default=None, max_length=300)
    # seller, site, buyer, location: codes from the reference tables; the target's columns hold 100.
    seller: str | None = Field(default=None, max_length=100)
    site: str | None = Field(default=None, max_length=100)
    buyer: str | None = Field(default=None, max_length=100)
    po: str | None = Field(default=None, max_length=100)  # the target's Order No column holds 100
    location: str | None = Field(default=None, max_length=100)
    date_printed: str | None = Field(default=None, max_length=40)  # a date as printed, month names included
    date: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=10)  # an ISO code, or a printed sign such as "Dhs"
    origin: str | None = Field(default=None, max_length=100)  # a country
    market: str | None = Field(default=None, max_length=100)
    taxCode: str | None = Field(default=None, max_length=100)  # the target's Tax Code column holds 100
    net: Decimal | None = None
    tax: Decimal | None = None
    lines: list[Line] = Field(default_factory=list, max_length=1000)


class EditedLine(Line):
    refuse_over_cap: ClassVar[bool] = True


class EditedInvoice(Invoice):
    """An invoice a member saved: a value over a field's cap is refused (422), never dropped."""
    refuse_over_cap: ClassVar[bool] = True
    lines: list[EditedLine] = Field(default_factory=list, max_length=1000)


class Policy(StrictModel):
    price_tolerance: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    total_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0, le=100)
    currency_decimals: dict[str, int] = Field(default_factory=lambda: {"AED": 2, "KWD": 3, "USD": 2, "EUR": 2})


class ProcessingOptions(StrictModel):
    engine: Literal["auto", "invoice2data", "paddleocr", "docling", "ai"] = "auto"
    prefer_native_text: bool = True
    ai_fallback: bool = True
    # An AI call on an invoice the local engines read completely: flags only, a cost, so off unless chosen.
    ai_cross_check: bool = False
    provider: Literal["openai", "anthropic", "azure", "chatgpt", "claude_local", "vertex"] = "openai"
    model: str = Field(default="", max_length=150, pattern=MODEL_NAME)
    language: Literal["en", "ar", "ch", "fr", "de"] = "en"


# Printed header fields an AI reader quotes with a page: the evidence sits beside the invoice, never inside it.
HEADER_EVIDENCE_FIELDS = ("number", "supplier_name", "buyer_name", "po", "date", "currency", "net", "tax")


def extraction_schema():
    # A deliberately simple schema shared by the two provider APIs. Monetary
    # values are strings so digits survive JSON decoding without float rounding.
    nullable = lambda: {"type": ["string", "null"]}
    fields = {k: nullable() for k in Invoice.model_fields if k != "lines"}
    line = {k: nullable() for k in Line.model_fields if k not in ("page", "part_code", "barcode_unchecked")}
    line["page"] = {"type": ["integer", "null"]}
    fields["lines"] = {"type": "array", "items": {"type": "object", "properties": line,
        "required": list(line), "additionalProperties": False}}
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def ai_schema():
    """The extraction schema plus header_evidence: one {quote, page} per printed header field."""
    schema = extraction_schema()
    entry = {"type": ["object", "null"], "properties": {"quote": {"type": ["string", "null"]},
             "page": {"type": ["integer", "null"]}}, "required": ["quote", "page"], "additionalProperties": False}
    schema["properties"]["header_evidence"] = {"type": ["object", "null"],
        "properties": {field: dict(entry) for field in HEADER_EVIDENCE_FIELDS},
        "required": list(HEADER_EVIDENCE_FIELDS), "additionalProperties": False}
    schema["required"] = list(schema["properties"])
    return schema


def parse_ai_output(data):
    """Split a reader's JSON (text or dict) into (Invoice, header evidence); evidence never enters the invoice."""
    import json
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ValueError("AI output is not an object")
    raw = data.pop("header_evidence", None)
    evidence = {}
    for field in HEADER_EVIDENCE_FIELDS:
        entry = (raw or {}).get(field) if isinstance(raw, dict) else None
        quote = entry.get("quote") if isinstance(entry, dict) else None
        page = entry.get("page") if isinstance(entry, dict) else None
        if isinstance(quote, str) and quote.strip():
            evidence[field] = {"quote": quote.strip()[:300],
                               "page": page if isinstance(page, int) and 1 <= page <= 20 else None}
    for line in data.get("lines") or []:
        if isinstance(line, dict):
            line.pop("part_code", None)  # a local table fact; an AI never supplies it
            line.pop("barcode_unchecked", None)  # the reader's verdict on a printed code; an AI never sets it
    # A reader can copy a printed value with the line break it was wrapped on; the value is
    # the same text on one line. Only whitespace is touched, never a character of the value.
    for holder in [data, *(line for line in (data.get("lines") or []) if isinstance(line, dict))]:
        for field, value in list(holder.items()):
            if isinstance(value, str) and value.strip() != " ".join(value.split()):
                holder[field] = " ".join(value.split())
    return Invoice.model_validate(data), evidence
