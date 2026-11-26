"""Deterministic validation: decides whether a record is clean (post) or doubtful (review).

The LLM proposes values; this module decides. Nothing here calls a model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal

from pydantic import BaseModel

from core.injection import detect_injection
from core.normalize import SUPPORTED_CURRENCIES, parse_amount, parse_date
from core.schema import (
    REQUIRED_FIELDS,
    TEXT_FIELDS,
    DocumentType,
    Issue,
    IssueCode,
    ReceiptRecord,
)

ZERO = Decimal(0)


@dataclass(frozen=True)
class ValidationConfig:
    abs_tolerance: Decimal = Decimal("0.02")
    rel_tolerance: Decimal = Decimal("0.001")  # 0.1%: covers cash rounding on large IDR totals
    max_tax_rate: Decimal = Decimal("0.30")
    max_age_days: int | None = 730  # None disables (the public datasets are from 2017-2019)
    min_ocr_confidence: float = 0.60  # mean word confidence, 0-1
    # Variance of the Laplacian at 800px width. Calibrated on 24 dataset images: originals
    # scored 22-4187, the same images with a heavy blur scored 1-6.
    min_blur_score: float = 10.0
    allowed_currencies: frozenset[str] = SUPPORTED_CURRENCIES


DEFAULT_CONFIG = ValidationConfig()


@dataclass
class DocumentContext:
    """Evidence about the source document, gathered outside the LLM."""

    ocr_text: str | None = None
    ocr_confidence: float | None = None
    blur_score: float | None = None
    is_duplicate: bool = False
    as_of: Date | None = None  # "today" for date checks; injectable for tests
    # Fields a human confirmed in chat. They skip grounding: the OCR text may be exactly
    # what was misread ("8.69" printed as "8.96"), so it can't be the judge any more.
    verified_fields: frozenset[str] = frozenset()


class ValidationResult(BaseModel):
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return not any(i.blocking for i in self.issues)

    @property
    def codes(self) -> set[str]:
        return {i.code.value for i in self.issues}


def validate_record(
    record: ReceiptRecord,
    context: DocumentContext | None = None,
    config: ValidationConfig = DEFAULT_CONFIG,
    prior_issues: Iterable[Issue] = (),
) -> ValidationResult:
    """Run every check. `prior_issues` carries issues found during normalization."""
    ctx = context or DocumentContext()
    issues: list[Issue] = list(prior_issues)

    if record.document_type == DocumentType.OTHER:
        issues.append(
            Issue(
                code=IssueCode.NOT_A_RECEIPT,
                message="Document does not look like a receipt or invoice.",
            )
        )
    else:
        issues += _check_required(record)
        issues += _check_amounts(record, config)
        issues += _check_date(record, ctx, config)
        issues += _check_currency(record, config)
        issues += _check_grounding(record, ctx)

    issues += _check_quality(ctx, config)
    issues += _check_injection(record, ctx)
    if ctx.is_duplicate:
        issues.append(
            Issue(
                code=IssueCode.DUPLICATE,
                message="A record with the same file or vendor/date/total was already posted.",
            )
        )
    return ValidationResult(issues=_dedupe(issues))


# --------------------------------------------------------------------------- checks


def _check_required(r: ReceiptRecord) -> list[Issue]:
    missing = [f for f in REQUIRED_FIELDS if getattr(r, f) in (None, "")]
    if r.document_type == DocumentType.INVOICE and not r.invoice_number:
        missing.append("invoice_number")
    return [
        Issue(code=IssueCode.MISSING_FIELD, field=f, message=f"Required field '{f}' is missing.")
        for f in missing
    ]


def _check_amounts(r: ReceiptRecord, cfg: ValidationConfig) -> list[Issue]:
    issues: list[Issue] = []
    total = r.total

    def close(a: Decimal, b: Decimal) -> bool:
        tol = max(cfg.abs_tolerance, cfg.rel_tolerance * max(abs(a), abs(b)))
        return abs(a - b) <= tol

    if total is not None and total <= ZERO:
        issues.append(
            Issue(
                code=IssueCode.BAD_AMOUNT, field="total", message=f"Total {total} is not positive."
            )
        )

    for i, li in enumerate(r.line_items):
        if li.quantity is not None and li.unit_price is not None and li.amount is not None:
            expected = li.quantity * li.unit_price
            if not close(expected, li.amount):
                issues.append(
                    Issue(
                        code=IssueCode.LINE_MATH,
                        field=f"line_items[{i}]",
                        message=(
                            f"'{li.description}': {li.quantity} x {li.unit_price} = {expected}, "
                            f"but amount is {li.amount}."
                        ),
                    )
                )

    tax = r.tax or ZERO
    # Discounts may be printed as "5.00" or "5.00-"; either way they reduce the total.
    adj = (r.service_charge or ZERO) + (r.rounding or ZERO) - abs(r.discount or ZERO)

    if r.subtotal is not None and total is not None:
        exclusive = r.subtotal + tax + adj  # tax added on top of the subtotal
        inclusive = r.subtotal + adj  # prices already include tax
        if not (close(exclusive, total) or close(inclusive, total)):
            issues.append(
                Issue(
                    code=IssueCode.TOTAL_MISMATCH,
                    field="total",
                    message=(
                        f"Subtotal {r.subtotal} + tax {tax} + adjustments {adj} = {exclusive}, "
                        f"which does not match total {total}."
                    ),
                )
            )

    amounts = [li.amount for li in r.line_items]
    if amounts and all(a is not None for a in amounts):
        items_sum = sum(amounts, ZERO)
        # Each target is a real receipt layout (tax-inclusive/exclusive, discount as a line, ...).
        targets = [r.subtotal]
        if total is not None:
            targets += [total, total - tax, total - adj, total - tax - adj]
        targets = [t for t in targets if t is not None]
        if targets and not any(close(items_sum, t) for t in targets):
            issues.append(
                Issue(
                    code=IssueCode.LINE_SUM_MISMATCH,
                    field="line_items",
                    message=(
                        f"Line items add up to {items_sum}, which matches neither the subtotal "
                        f"({r.subtotal}) nor the total ({total})."
                    ),
                )
            )

    if r.tax is not None:
        base = r.subtotal if r.subtotal else (total - r.tax if total is not None else None)
        if r.tax < ZERO or (base and base > ZERO and r.tax / base > cfg.max_tax_rate):
            issues.append(
                Issue(
                    code=IssueCode.TAX_RATE_ODD,
                    field="tax",
                    message=f"Tax {r.tax} is implausible for a taxable base of {base}.",
                )
            )
    return issues


def _check_date(r: ReceiptRecord, ctx: DocumentContext, cfg: ValidationConfig) -> list[Issue]:
    if r.date is None:
        return []
    today = ctx.as_of or Date.today()
    if r.date > today:
        return [
            Issue(code=IssueCode.BAD_DATE, field="date", message=f"Date {r.date} is in the future.")
        ]
    if cfg.max_age_days is not None and (today - r.date).days > cfg.max_age_days:
        return [
            Issue(
                code=IssueCode.STALE_DATE,
                field="date",
                message=f"Date {r.date} is older than {cfg.max_age_days} days.",
            )
        ]
    return []


def _check_currency(r: ReceiptRecord, cfg: ValidationConfig) -> list[Issue]:
    if r.currency and r.currency not in cfg.allowed_currencies:
        return [
            Issue(
                code=IssueCode.BAD_CURRENCY,
                field="currency",
                message=f"Currency {r.currency} is not supported.",
            )
        ]
    return []


def _check_grounding(r: ReceiptRecord, ctx: DocumentContext) -> list[Issue]:
    """Every key value must actually appear in the document text. Catches hallucinated or
    injected values that the model 'read' but that are not printed."""
    text = ctx.ocr_text
    if not text or not text.strip():
        return []
    issues: list[Issue] = []
    printed_amounts = amounts_in_text(text, r.currency)
    for f in ("subtotal", "tax", "total"):
        value = getattr(r, f)
        if f in ctx.verified_fields:
            continue
        if value is not None and abs(value) not in printed_amounts:
            issues.append(
                Issue(
                    code=IssueCode.UNGROUNDED_VALUE,
                    field=f,
                    message=f"{f} {value} does not appear anywhere in the document text.",
                )
            )
    if (
        "date" not in ctx.verified_fields
        and r.date is not None
        and r.date not in dates_in_text(text)
    ):
        issues.append(
            Issue(
                code=IssueCode.UNGROUNDED_VALUE,
                field="date",
                message=f"Date {r.date} does not appear anywhere in the document text.",
            )
        )
    return issues


def _check_quality(ctx: DocumentContext, cfg: ValidationConfig) -> list[Issue]:
    issues: list[Issue] = []
    if ctx.ocr_confidence is not None and ctx.ocr_confidence < cfg.min_ocr_confidence:
        issues.append(
            Issue(
                code=IssueCode.LOW_QUALITY,
                message=(
                    f"OCR confidence {ctx.ocr_confidence:.2f} is below {cfg.min_ocr_confidence}."
                ),
            )
        )
    if ctx.blur_score is not None and ctx.blur_score < cfg.min_blur_score:
        issues.append(
            Issue(
                code=IssueCode.LOW_QUALITY,
                message=(
                    f"Image is blurry (sharpness {ctx.blur_score:.0f} < {cfg.min_blur_score:.0f})."
                ),
            )
        )
    return issues


def _check_injection(r: ReceiptRecord, ctx: DocumentContext) -> list[Issue]:
    texts = [ctx.ocr_text] + [getattr(r, f) for f in TEXT_FIELDS]
    texts += [li.description for li in r.line_items]
    hits = sorted({h for t in texts for h in detect_injection(t)})
    if not hits:
        return []
    return [
        Issue(
            code=IssueCode.POSSIBLE_INJECTION,
            message=f"Instruction-like text found in the document ({', '.join(hits)}).",
        )
    ]


# --------------------------------------------------------------------------- helpers

_NUMBER_TOKEN = re.compile(r"\d[\d.,]*\d|\d")
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_CANDIDATES = [
    # No trailing \b: OCR often glues the time onto the date ("02/11/202614:05").
    re.compile(r"\b\d{1,4}[/\-.]\d{1,2}[/\-.]\d{4}"),
    re.compile(r"\b\d{1,4}[/\-.]\d{1,2}[/\-.]\d{2}\b"),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?[\s\-/]*{_MONTHS}[\s\-/,]*\d{{2,4}}\b", re.I),
    re.compile(rf"\b{_MONTHS}[\s\-/]*\d{{1,2}}(?:st|nd|rd|th)?[\s,\-/]*\d{{2,4}}\b", re.I),
]


def amounts_in_text(text: str, currency: str | None = None) -> set[Decimal]:
    found: set[Decimal] = set()
    for token in _NUMBER_TOKEN.findall(text):
        value = parse_amount(token, currency)
        if value is not None:
            found.add(abs(value))
    return found


def dates_in_text(text: str) -> set[Date]:
    found: set[Date] = set()
    for rx in _DATE_CANDIDATES:
        for m in rx.finditer(text):
            d = parse_date(m.group(0))
            if d is not None:
                found.add(d)
    return found


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen: set[tuple[str, str | None, str]] = set()
    out: list[Issue] = []
    for i in issues:
        key = (i.code.value, i.field, i.message)
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out
