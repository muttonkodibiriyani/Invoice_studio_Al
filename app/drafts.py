"""Strict, side-effect-free creation of manually entered draft workbooks.

Manual drafts deliberately do not participate in reference approval or the
receipt reservation ledger.  The API layer may audit the public metadata
returned here, but this module only validates submitted values and builds
bytes in memory.
"""

from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Mapping

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .excel import HEADERS, text_cell


_DISCLOSURE = (
    "DRAFT_UNVALIDATED: Values were entered manually, reference data was not "
    "validated, and no receipt number was reserved."
)
_FORMULA_PREFIX = re.compile(r"^[=+\-@]")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_PLAIN_DECIMAL = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d{1,4})?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FOUR_PLACES = Decimal("0.0001")


def _safe_text(value: Any, *, required: bool) -> str:
    if not isinstance(value, str):
        raise ValueError("must be text")
    value = value.strip()
    if required and not value:
        raise ValueError("must not be blank")
    if _CONTROL_CHARACTER.search(value):
        raise ValueError("must not contain control characters")
    if _FORMULA_PREFIX.match(value):
        raise ValueError("must not begin with a spreadsheet formula character")
    return value


def _decimal(value: Any) -> Decimal:
    # JSON floats have already lost their exact decimal spelling.  The browser
    # sends input values as strings, while integer literals remain safe.
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("must be an exact decimal string or integer")
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, Decimal):
        value = format(value, "f")
    if not isinstance(value, str):
        raise ValueError("must be an exact decimal string or integer")
    value = value.strip()
    if not _PLAIN_DECIMAL.fullmatch(value):
        raise ValueError("must be a plain non-negative decimal with at most 4 places")
    return Decimal(value)


def _document_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("must be an ISO date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a real calendar date") from exc


class _DraftModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        str_strip_whitespace=True,
        validate_by_alias=True,
        validate_by_name=False,
    )


class DraftHeader(_DraftModel):
    transaction_number: int = Field(alias="Transaction Number", strict=True, ge=1, le=1)
    document: str = Field(alias="Document", min_length=1, max_length=200)
    supplier_site: str = Field(alias="Supplier Site", min_length=1, max_length=100)
    order_no: str = Field(alias="Order No", min_length=1, max_length=100)
    location: str = Field(alias="Location", min_length=1, max_length=100)
    location_type: Literal["Store (S)", "Warehouse (W)"] = Field(alias="Location Type")
    document_date: date = Field(alias="Document Date")
    total_cost_ex_tax: Decimal = Field(
        alias="Total Cost Ex Tax", ge=0, max_digits=18, decimal_places=4
    )
    tax_amount: Decimal = Field(alias="Tax Amount", ge=0, max_digits=18, decimal_places=4)
    ref_no_1: str = Field(alias="Ref No. 1", max_length=200)
    ref_no_2: str = Field(alias="Ref No. 2", max_length=200)
    ref_no_3: str = Field(alias="Ref No. 3", max_length=200)
    comment: str = Field(alias="Comment", max_length=500)

    @field_validator("document", "supplier_site", "order_no", "location", mode="before")
    @classmethod
    def validate_required_text(cls, value: Any) -> str:
        return _safe_text(value, required=True)

    @field_validator("ref_no_1", "ref_no_2", "ref_no_3", "comment", mode="before")
    @classmethod
    def validate_optional_text(cls, value: Any) -> str:
        return _safe_text(value, required=False)

    @field_validator("document_date", mode="before")
    @classmethod
    def validate_date(cls, value: Any) -> date:
        return _document_date(value)

    @field_validator("total_cost_ex_tax", "tax_amount", mode="before")
    @classmethod
    def validate_decimal(cls, value: Any) -> Decimal:
        return _decimal(value)


class DraftTaxBreakdown(_DraftModel):
    transaction_number: int = Field(alias="Transaction Number", strict=True, ge=1, le=1)
    tax_code: str = Field(alias="Tax Code", min_length=1, max_length=100)
    tax_basis: Decimal = Field(alias="Tax Basis", ge=0, max_digits=18, decimal_places=4)

    @field_validator("tax_code", mode="before")
    @classmethod
    def validate_tax_code(cls, value: Any) -> str:
        return _safe_text(value, required=True)

    @field_validator("tax_basis", mode="before")
    @classmethod
    def validate_decimal(cls, value: Any) -> Decimal:
        return _decimal(value)


class DraftDetail(_DraftModel):
    transaction_number: int = Field(alias="Transaction Number", strict=True, ge=1, le=1)
    item: str = Field(alias="Item", min_length=1, max_length=200)
    upc: str = Field(alias="UPC", min_length=1, max_length=100)
    unit_cost: Decimal = Field(alias="Unit Cost", ge=0, max_digits=18, decimal_places=4)
    quantity: Decimal = Field(alias="Quantity", gt=0, max_digits=18, decimal_places=4)
    unit_tax_code: str = Field(alias="Unit Tax Code", min_length=1, max_length=100)

    @field_validator("item", "upc", "unit_tax_code", mode="before")
    @classmethod
    def validate_required_text(cls, value: Any) -> str:
        return _safe_text(value, required=True)

    @field_validator("unit_cost", "quantity", mode="before")
    @classmethod
    def validate_decimal(cls, value: Any) -> Decimal:
        return _decimal(value)


class ManualDraftRequest(_DraftModel):
    """A complete one-transaction snapshot submitted by the manual editor."""

    acknowledge_unvalidated: Literal[True]
    header: DraftHeader = Field(alias="Header")
    tax_breakdown: DraftTaxBreakdown = Field(alias="Tax_Breakdown")
    details: list[DraftDetail] = Field(alias="Details", min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_arithmetic(self) -> "ManualDraftRequest":
        expected = self.header.total_cost_ex_tax.quantize(_FOUR_PLACES)
        if self.tax_breakdown.tax_basis.quantize(_FOUR_PLACES) != expected:
            raise ValueError("Tax Basis must equal Total Cost Ex Tax")
        detail_total = sum(
            (detail.unit_cost * detail.quantity for detail in self.details),
            start=Decimal("0"),
        ).quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)
        if detail_total != expected:
            raise ValueError("detail Unit Cost × Quantity total must equal Total Cost Ex Tax")
        return self


def validate_draft(payload: ManualDraftRequest | Mapping[str, Any]) -> ManualDraftRequest:
    """Return the validated model used to generate a draft workbook."""

    if isinstance(payload, ManualDraftRequest):
        return payload
    return ManualDraftRequest.model_validate(payload)


def _style_sheet(workbook: Workbook, name: str) -> None:
    sheet = workbook.create_sheet(name)
    sheet.append(HEADERS[name])
    sheet.freeze_panes = "A2"
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="173D3B")
    for cell in sheet[1]:
        sheet.column_dimensions[cell.column_letter].width = max(18, len(str(cell.value)) + 2)
    sheet["A1"].comment = Comment(_DISCLOSURE, "Invoice Studio")


def build_draft_workbook(payload: ManualDraftRequest | Mapping[str, Any]) -> bytes:
    """Build an exact three-sheet target workbook without external state reads."""

    draft = validate_draft(payload)
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name in HEADERS:
        _style_sheet(workbook, name)

    workbook.properties.title = "Invoice Studio DRAFT_UNVALIDATED"
    workbook.properties.subject = "Manual invoice draft"
    workbook.properties.category = "DRAFT_UNVALIDATED"
    workbook.properties.keywords = "draft,unvalidated,no receipt reservation"
    workbook.properties.description = _DISCLOSURE
    workbook.properties.creator = "Invoice Studio"

    header = workbook["Header"]
    header_values = [
        1,
        draft.header.document,
        draft.header.supplier_site,
        draft.header.order_no,
        draft.header.location,
        draft.header.location_type,
        draft.header.document_date,
        draft.header.total_cost_ex_tax,
        draft.header.tax_amount,
        draft.header.ref_no_1,
        draft.header.ref_no_2,
        draft.header.ref_no_3,
        draft.header.comment,
    ]
    for column, value in enumerate(header_values, start=1):
        cell = header.cell(2, column)
        if column in (2, 3, 4, 5, 6, 10, 11, 12, 13):
            text_cell(cell, value)
        else:
            cell.value = value
    header.cell(2, 7).number_format = "m/d/yyyy"
    for column in (8, 9):
        header.cell(2, column).number_format = "0.0000"

    tax = workbook["Tax_Breakdown"]
    tax.cell(2, 1, 1)
    text_cell(tax.cell(2, 2), draft.tax_breakdown.tax_code)
    tax.cell(2, 3, draft.tax_breakdown.tax_basis).number_format = "0.0000"

    details = workbook["Details"]
    for row, detail in enumerate(draft.details, start=2):
        details.cell(row, 1, 1)
        text_cell(details.cell(row, 2), detail.item)
        text_cell(details.cell(row, 3), detail.upc)
        details.cell(row, 3).number_format = "@"
        details.cell(row, 4, detail.unit_cost).number_format = "0.0000"
        details.cell(row, 5, detail.quantity).number_format = "0.0000"
        text_cell(details.cell(row, 6), detail.unit_tax_code)

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def draft_filename(payload: ManualDraftRequest | Mapping[str, Any]) -> str:
    draft = validate_draft(payload)
    document = re.sub(r"[^A-Za-z0-9._-]+", "_", draft.header.document).strip("._-")
    document = document[:80] or "invoice"
    return f"Invoice_{document}_DRAFT_UNVALIDATED.xlsx"


def draft_public_metadata(
    payload: ManualDraftRequest | Mapping[str, Any], *, job_id: str | None = None
) -> dict[str, Any]:
    """Return non-sensitive response/audit facts; no invoice values are exposed."""

    draft = validate_draft(payload)
    metadata: dict[str, Any] = {
        "kind": "manual_draft",
        "draft": True,
        "reference_validated": False,
        "receipt_reserved": False,
        "transaction_number": 1,
        "detail_rows": len(draft.details),
        "filename": draft_filename(draft),
    }
    if job_id is not None:
        metadata["job_id"] = job_id
    return metadata
