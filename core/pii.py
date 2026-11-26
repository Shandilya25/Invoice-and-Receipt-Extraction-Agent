"""PII masking for anything that is logged or traced.

Receipts carry card numbers, phone numbers, emails, tax IDs and names. Logs and traces
must never contain them in clear text. Amounts and dates are kept: they are needed for
debugging and are not personal data on their own.
"""

from __future__ import annotations

import logging
import re

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(\.[\w-]+)+\b")
# 12-19 digits, optionally grouped by spaces/dashes: card, bank account, tax/registration IDs.
_LONG_DIGITS = re.compile(r"(?<![\d.,])(?:\d[ -]?){11,18}\d(?![\d.,])")
# Phone-like: digit groups separated by spaces/dashes/brackets, checked for 9-11 digits below.
_PHONE = re.compile(r"(?<![\w.,/])\+?\(?\d{1,4}\)?(?:[ -]?\d{2,4}){2,4}(?![\w.,/])")
_LABELLED_NAME = re.compile(
    r"(?im)\b(name|customer|cust|bill\s*to|attn|cashier|served\s*by)(\s*[:#-]\s*)([^\n\r]{1,60})"
)


def _mask_long_digits(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "*" * (len(digits) - 4) + digits[-4:]


def _mask_phone(m: re.Match[str]) -> str:
    n = sum(ch.isdigit() for ch in m.group(0))
    return "[PHONE]" if 9 <= n <= 11 else m.group(0)


def mask_pii(text: str | None) -> str:
    if not text:
        return text or ""
    out = _EMAIL.sub("[EMAIL]", text)
    out = _LONG_DIGITS.sub(_mask_long_digits, out)
    out = _PHONE.sub(_mask_phone, out)
    out = _LABELLED_NAME.sub(lambda m: f"{m.group(1)}{m.group(2)}[NAME]", out)
    return out


class PIIMaskingFilter(logging.Filter):
    """Logging filter that masks PII in the fully formatted message of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_pii(record.getMessage())
        record.args = None
        return True
