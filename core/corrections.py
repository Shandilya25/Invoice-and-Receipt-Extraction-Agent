"""Applying a user's chat correction ("the subtotal is 8.96") to a record.

User text is untrusted too: only allow-listed fields can be changed, every value goes
through the same parsers as extracted values, and the caller must re-validate afterwards.
There is deliberately no way to change status or skip validation from here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from core.normalize import parse_amount, parse_currency, parse_date, parse_quantity
from core.schema import AMOUNT_FIELDS, TEXT_FIELDS, DocumentType, ReceiptRecord

_LINE_ITEM_FIELD = re.compile(r"^line_items\[(\d+)\]\.(description|quantity|unit_price|amount)$")
_CLEAR_WORDS = {"", "null", "none", "n/a", "clear", "remove"}

CORRECTABLE_FIELDS = frozenset({*AMOUNT_FIELDS, *TEXT_FIELDS, "date", "currency", "document_type"})


class CorrectionError(ValueError):
    """The correction was rejected; the message is safe to show to the user."""


@dataclass(frozen=True)
class Correction:
    field: str
    old_value: Any
    new_value: Any
    record: ReceiptRecord


def apply_correction(record: ReceiptRecord, field: str, value: str | None) -> Correction:
    """Return a new record with `field` set to the parsed `value` (the input is not mutated)."""
    field = (field or "").strip()
    raw = "" if value is None else str(value).strip()

    m = _LINE_ITEM_FIELD.match(field)
    if m:
        return _correct_line_item(record, int(m[1]), m[2], raw)

    if field not in CORRECTABLE_FIELDS:
        allowed = ", ".join(sorted(CORRECTABLE_FIELDS))
        raise CorrectionError(f"'{field}' cannot be corrected. Correctable fields: {allowed}.")

    new_value = _parse_for_field(field, raw, record.currency)
    old_value = getattr(record, field)
    data = record.model_dump()
    data[field] = new_value
    return Correction(field, old_value, new_value, _rebuild(data))


def _correct_line_item(record: ReceiptRecord, idx: int, attr: str, raw: str) -> Correction:
    if idx >= len(record.line_items):
        raise CorrectionError(
            f"There is no line item #{idx} (record has {len(record.line_items)})."
        )
    if attr == "description":
        if raw.lower() in _CLEAR_WORDS:
            raise CorrectionError("A line item description cannot be empty.")
        new_value: Any = raw
    elif attr == "quantity":
        new_value = None if raw.lower() in _CLEAR_WORDS else parse_quantity(raw)
        if raw.lower() not in _CLEAR_WORDS and new_value is None:
            raise CorrectionError(f"Could not read {raw!r} as a quantity.")
    else:
        new_value = _parse_for_field("total", raw, record.currency)  # same rules as money
    data = record.model_dump()
    old_value = data["line_items"][idx][attr]
    data["line_items"][idx][attr] = new_value
    return Correction(f"line_items[{idx}].{attr}", old_value, new_value, _rebuild(data))


def _parse_for_field(field: str, raw: str, currency: str | None) -> Any:
    if raw.lower() in _CLEAR_WORDS:
        if field == "document_type":
            raise CorrectionError("document_type cannot be cleared.")
        return None
    if field in AMOUNT_FIELDS:
        parsed = parse_amount(raw, currency)
        if parsed is None:
            raise CorrectionError(f"Could not read {raw!r} as an amount for {field}.")
        return parsed
    if field == "date":
        parsed_date = parse_date(raw)
        if parsed_date is None:
            raise CorrectionError(f"Could not read {raw!r} as a full date (day, month and year).")
        return parsed_date
    if field == "currency":
        code = parse_currency(raw)
        if code is None:
            raise CorrectionError(f"Unknown currency {raw!r}.")
        return code
    if field == "document_type":
        try:
            return DocumentType(raw.lower())
        except ValueError:
            raise CorrectionError(
                f"document_type must be one of: {', '.join(t.value for t in DocumentType)}."
            ) from None
    return raw  # free-text field; length limits enforced by the schema


def _rebuild(data: dict[str, Any]) -> ReceiptRecord:
    try:
        return ReceiptRecord.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise CorrectionError(f"Invalid value for {loc}: {first['msg']}.") from None
