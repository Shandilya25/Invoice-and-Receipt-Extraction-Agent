from datetime import date
from decimal import Decimal

import pytest

from core.corrections import CorrectionError, apply_correction
from core.normalize import normalize_record


@pytest.fixture
def record(myr_raw):
    return normalize_record(myr_raw)[0]


def test_correct_amount_keeps_audit_values(record):
    fix = apply_correction(record, "subtotal", "RM 8.69")
    assert fix.field == "subtotal"
    assert fix.old_value == Decimal("8.96")
    assert fix.new_value == Decimal("8.69")
    assert fix.record.subtotal == Decimal("8.69")
    assert record.subtotal == Decimal("8.96")  # original untouched


def test_correct_date_and_currency_and_vendor(record):
    assert apply_correction(record, "date", "15 March 2018").record.date == date(2018, 3, 15)
    assert apply_correction(record, "currency", "Rp").record.currency == "IDR"
    assert apply_correction(record, "vendor_name", "  Maju Stationery  ").record.vendor_name == (
        "Maju Stationery"
    )


def test_correct_line_item(record):
    fix = apply_correction(record, "line_items[1].amount", "6.00")
    assert fix.field == "line_items[1].amount"
    assert fix.old_value == Decimal("5.96")
    assert fix.record.line_items[1].amount == Decimal("6.00")


def test_clear_optional_field(record):
    assert apply_correction(record, "tax", "none").record.tax is None


@pytest.mark.parametrize(
    ("field", "value", "msg"),
    [
        ("status", "posted", "cannot be corrected"),  # no way to force a status
        ("validation", "skip", "cannot be corrected"),
        ("total", "about ten", "as an amount"),
        ("date", "14/03", "full date"),
        ("currency", "dogecoin", "Unknown currency"),
        ("document_type", "selfie", "must be one of"),
        ("document_type", "", "cannot be cleared"),
        ("line_items[9].amount", "1.00", "no line item #9"),
        ("line_items[0].description", "", "cannot be empty"),
        ("vendor_name", "x" * 400, "Invalid value"),
    ],
)
def test_rejected_corrections(record, field, value, msg):
    with pytest.raises(CorrectionError, match=msg):
        apply_correction(record, field, value)
