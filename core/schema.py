"""Data contracts for the receipt pipeline.

Two stages:
  RawExtraction  - what the LLM returns: every value copied *exactly as printed* (strings).
  ReceiptRecord  - the typed, normalized record built by deterministic code (normalize.py).

The LLM only has to copy what it sees; number/date parsing happens in code, where it is
testable and locale-aware (e.g. Indonesian "60.000" means 60000, not 60.0).
"""

from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TEXT = 300
MAX_LINE_ITEMS = 200
_NULL_STRINGS = {"", "null", "none", "n/a", "na", "-"}


class DocumentType(StrEnum):
    RECEIPT = "receipt"
    INVOICE = "invoice"
    OTHER = "other"


class RecordStatus(StrEnum):
    PENDING = "pending"
    POSTED = "posted"
    NEEDS_REVIEW = "needs_review"


class IssueCode(StrEnum):
    MISSING_FIELD = "MISSING_FIELD"
    UNPARSEABLE_VALUE = "UNPARSEABLE_VALUE"
    BAD_AMOUNT = "BAD_AMOUNT"
    LINE_MATH = "LINE_MATH"
    LINE_SUM_MISMATCH = "LINE_SUM_MISMATCH"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    TAX_RATE_ODD = "TAX_RATE_ODD"
    BAD_DATE = "BAD_DATE"
    STALE_DATE = "STALE_DATE"
    BAD_CURRENCY = "BAD_CURRENCY"
    CURRENCY_INFERRED = "CURRENCY_INFERRED"
    UNGROUNDED_VALUE = "UNGROUNDED_VALUE"
    LOW_QUALITY = "LOW_QUALITY"
    DUPLICATE = "DUPLICATE"
    POSSIBLE_INJECTION = "POSSIBLE_INJECTION"
    NOT_A_RECEIPT = "NOT_A_RECEIPT"


class Issue(BaseModel):
    """One reason a record is doubtful. Blocking issues send the record to human review."""

    model_config = ConfigDict(frozen=True)

    code: IssueCode
    message: str
    field: str | None = None
    blocking: bool = True


# --------------------------------------------------------------------------- raw (LLM output)


class _RawBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in _NULL_STRINGS:
            return None
        return v


class RawLineItem(_RawBase):
    description: str = Field(min_length=1, max_length=MAX_TEXT)
    quantity: str | None = Field(default=None, max_length=40)
    unit_price: str | None = Field(default=None, max_length=40)
    amount: str | None = Field(default=None, max_length=40)


class RawExtraction(_RawBase):
    """Exactly what is printed on the document. Use null when a value is not printed."""

    document_type: DocumentType
    vendor_name: str | None = Field(default=None, max_length=MAX_TEXT)
    vendor_address: str | None = Field(default=None, max_length=MAX_TEXT)
    vendor_tax_id: str | None = Field(default=None, max_length=60)
    invoice_number: str | None = Field(default=None, max_length=60)
    date: str | None = Field(default=None, max_length=60)
    currency: str | None = Field(default=None, max_length=10)
    line_items: list[RawLineItem] = Field(default_factory=list, max_length=MAX_LINE_ITEMS)
    subtotal: str | None = Field(default=None, max_length=40)
    tax: str | None = Field(default=None, max_length=40)
    service_charge: str | None = Field(default=None, max_length=40)
    discount: str | None = Field(default=None, max_length=40)
    rounding: str | None = Field(default=None, max_length=40)
    total: str | None = Field(default=None, max_length=40)
    payment_method: str | None = Field(default=None, max_length=60)


# --------------------------------------------------------------------------- typed record


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    description: str = Field(min_length=1, max_length=MAX_TEXT)
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


class ReceiptRecord(BaseModel):
    """Normalized record. `tax=None` means no tax line was printed; `Decimal(0)` means 0 printed."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    document_type: DocumentType
    vendor_name: str | None = Field(default=None, max_length=MAX_TEXT)
    vendor_address: str | None = Field(default=None, max_length=MAX_TEXT)
    vendor_tax_id: str | None = Field(default=None, max_length=60)
    invoice_number: str | None = Field(default=None, max_length=60)
    date: Date | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    line_items: list[LineItem] = Field(default_factory=list, max_length=MAX_LINE_ITEMS)
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    service_charge: Decimal | None = None
    discount: Decimal | None = None
    rounding: Decimal | None = None
    total: Decimal | None = None
    payment_method: str | None = Field(default=None, max_length=60)


AMOUNT_FIELDS = ("subtotal", "tax", "service_charge", "discount", "rounding", "total")
TEXT_FIELDS = ("vendor_name", "vendor_address", "vendor_tax_id", "invoice_number", "payment_method")
REQUIRED_FIELDS = ("vendor_name", "date", "total", "currency")
