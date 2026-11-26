"""Deterministic parsing of values copied from receipts: amounts, dates, currencies, vendors.

All functions return None instead of guessing when a value is ambiguous or unreadable.
"""

from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from dateutil import parser as date_parser

from core.schema import (
    AMOUNT_FIELDS,
    Issue,
    IssueCode,
    LineItem,
    RawExtraction,
    ReceiptRecord,
)

CENT = Decimal("0.01")

# Printed symbol / code -> ISO 4217
_CURRENCY_ALIASES = {
    "RM": "MYR",
    "MYR": "MYR",
    "RP": "IDR",
    "RP.": "IDR",
    "IDR": "IDR",
    "S$": "SGD",
    "SGD": "SGD",
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "₹": "INR",
    "RS": "INR",
    "RS.": "INR",
    "INR": "INR",
}
SUPPORTED_CURRENCIES = frozenset(_CURRENCY_ALIASES.values())

# Currencies printed without minor units, where a single "." or "," is a thousands separator.
_NO_MINOR_UNIT = frozenset({"IDR", "JPY", "KRW", "VND"})

_VENDOR_SUFFIXES = re.compile(
    r"\b(SDN\.?\s*BHD\.?|BHD\.?|PLT|PT\.?|TBK\.?|LTD\.?|LIMITED|INC\.?|LLC|CO\.?)\b"
)


# --------------------------------------------------------------------------- amounts


def parse_amount(text: str | None, currency: str | None = None) -> Decimal | None:
    """Parse a printed money amount into a Decimal rounded to cents.

    Handles "RM 9.50", "1,234.56", "1.234,56", "Rp 60.000" (=60000), "(5.00)", "5.00-".
    Returns None for empty, unreadable, or multi-number strings.
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None

    negative = (
        (s.startswith("(") and s.endswith(")"))
        or s.startswith("-")  # "-RM 5.00"
        or re.search(r"-\s*[\d.,]", s) is not None  # "-5.00", "RM -5.00"
        or s.endswith("-")  # "5.00-" (common on receipts for discounts)
    )
    s = re.sub(r"[^\d.,\s']", "", s).strip()  # drop currency symbols/letters/signs
    s = s.replace("'", "")
    if not s or not any(ch.isdigit() for ch in s):
        return None

    # Spaces are only allowed as thousands separators ("1 234 567.89"); otherwise it's
    # two numbers ("2 3.50") and we refuse to guess.
    if re.search(r"\s", s):
        if not re.fullmatch(r"\d{1,3}(\s\d{3})+([.,]\d+)?", s):
            return None
        s = re.sub(r"\s", "", s)

    s = s.strip(".,")
    if not re.fullmatch(r"\d[\d.,]*", s):
        return None

    s = _resolve_separators(s, currency)
    if s is None:
        return None
    try:
        value = Decimal(s).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return -value if negative else value


def _resolve_separators(s: str, currency: str | None) -> str | None:
    """Return a plain "1234.56" string, working out which separator is the decimal point."""
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        decimal_sep = "." if s.rfind(".") > s.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        integer, _, frac = s.rpartition(decimal_sep)
        return integer.replace(thousands_sep, "") + "." + frac

    sep = "." if has_dot else "," if has_comma else None
    if sep is None:
        return s
    parts = s.split(sep)
    if len(parts) > 2:  # "1.250.000" -> thousands separators
        if not all(len(p) == 3 for p in parts[1:]):
            return None
        return "".join(parts)
    integer, frac = parts
    if currency in _NO_MINOR_UNIT:
        return integer + frac
    if len(frac) == 3 and integer.lstrip("0"):  # "60.000" / "12,500" -> thousands
        return integer + frac
    return f"{integer}.{frac}"  # "9.50", "12,5", "0.535"


def parse_quantity(text: str | None) -> Decimal | None:
    """Parse a quantity ("2", "x2", "0.500", "1,5"). Unlike money, a single separator is
    always a decimal point - "0.500" kg is half a kilo, not 500."""
    if text is None:
        return None
    s = re.sub(r"[^\d.,]", "", str(text))
    if not s or s.count(".") + s.count(",") > 1:
        return None
    try:
        value = Decimal(s.replace(",", "."))
    except InvalidOperation:
        return None
    return value if value > 0 else None


# --------------------------------------------------------------------------- dates

_ISO_DATE = re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_DEFAULT_A = datetime(1, 1, 1)
_DEFAULT_B = datetime(2, 2, 2)


def parse_date(text: str | None, dayfirst: bool = True) -> Date | None:
    """Parse a printed date. Day-first by default (Malaysia/Indonesia print DD/MM/YYYY).

    Returns None if the year, month or day is missing ("25/12"), or the text is not a date.
    """
    if not text or not str(text).strip():
        return None
    s = str(text).strip()

    m = _ISO_DATE.search(s)  # year-first is unambiguous; dateutil+dayfirst would swap it
    if m:
        try:
            return _plausible(Date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            return None

    try:
        a = date_parser.parse(s, dayfirst=dayfirst, fuzzy=True, default=_DEFAULT_A)
        b = date_parser.parse(s, dayfirst=dayfirst, fuzzy=True, default=_DEFAULT_B)
    except (ValueError, OverflowError, TypeError):
        return None
    if a.date() != b.date():  # some component came from the default -> not printed
        return None
    return _plausible(a.date())


def _plausible(d: Date) -> Date | None:
    return d if 1990 <= d.year <= 2100 else None


# --------------------------------------------------------------------------- currency / vendor


def parse_currency(text: str | None) -> str | None:
    if not text:
        return None
    key = str(text).strip().upper()
    return _CURRENCY_ALIASES.get(key)


def vendor_key(name: str | None) -> str:
    """Canonical vendor string for duplicate detection and fuzzy comparison."""
    if not name:
        return ""
    s = name.upper().replace("&", " AND ")
    s = _VENDOR_SUFFIXES.sub(" ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- raw -> record


def normalize_record(
    raw: RawExtraction, default_currency: str | None = None
) -> tuple[ReceiptRecord, list[Issue]]:
    """Turn the LLM's as-printed strings into a typed record.

    Values that were printed but cannot be parsed become None plus an UNPARSEABLE_VALUE issue,
    so they are never silently dropped.
    """
    issues: list[Issue] = []

    currency = parse_currency(raw.currency)
    if raw.currency and currency is None:
        issues.append(_unparseable("currency", raw.currency))
    if currency is None and default_currency:
        currency = default_currency
        issues.append(
            Issue(
                code=IssueCode.CURRENCY_INFERRED,
                field="currency",
                message=f"No currency printed; assumed home currency {default_currency}.",
                blocking=False,
            )
        )

    def amount(field: str, value: str | None) -> Decimal | None:
        parsed = parse_amount(value, currency)
        if value is not None and parsed is None:
            issues.append(_unparseable(field, value))
        return parsed

    parsed_date = parse_date(raw.date)
    if raw.date and parsed_date is None:
        issues.append(_unparseable("date", raw.date))

    def quantity(field: str, value: str | None) -> Decimal | None:
        parsed = parse_quantity(value)
        if value is not None and parsed is None:
            issues.append(_unparseable(field, value))
        return parsed

    items = [
        LineItem(
            description=li.description,
            quantity=quantity(f"line_items[{i}].quantity", li.quantity),
            unit_price=amount(f"line_items[{i}].unit_price", li.unit_price),
            amount=amount(f"line_items[{i}].amount", li.amount),
        )
        for i, li in enumerate(raw.line_items)
    ]

    record = ReceiptRecord(
        document_type=raw.document_type,
        vendor_name=raw.vendor_name,
        vendor_address=raw.vendor_address,
        vendor_tax_id=raw.vendor_tax_id,
        invoice_number=raw.invoice_number,
        date=parsed_date,
        currency=currency,
        line_items=items,
        payment_method=raw.payment_method,
        **{f: amount(f, getattr(raw, f)) for f in AMOUNT_FIELDS},
    )
    return record, issues


def _unparseable(field: str, value: str) -> Issue:
    return Issue(
        code=IssueCode.UNPARSEABLE_VALUE,
        field=field,
        message=f"Could not read {field} from printed value {value!r}.",
    )
