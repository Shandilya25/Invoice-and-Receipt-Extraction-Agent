from datetime import date
from decimal import Decimal

import pytest

from core.normalize import (
    normalize_record,
    parse_amount,
    parse_currency,
    parse_date,
    parse_quantity,
    vendor_key,
)
from core.schema import IssueCode, RawExtraction


@pytest.mark.parametrize(
    ("text", "currency", "expected"),
    [
        ("9.50", None, "9.50"),
        ("RM 9.50", None, "9.50"),
        ("RM9.5", None, "9.50"),
        ("1,234.56", None, "1234.56"),
        ("1.234,56", None, "1234.56"),
        ("60.000", None, "60000.00"),  # CORD style: dot is a thousands separator
        ("60.000", "IDR", "60000.00"),
        ("Rp 1.250.000", None, "1250000.00"),
        ("12,500", None, "12500.00"),
        ("12,5", None, "12.50"),
        ("1 234.50", None, "1234.50"),
        ("(5.00)", None, "-5.00"),
        ("5.00-", None, "-5.00"),
        ("-RM 5.00", None, "-5.00"),
        ("RM -5.00", None, "-5.00"),
        ("0.535", None, "0.54"),  # leading zero can't be a thousands group
        ("1.5", "IDR", "15.00"),  # IDR has no minor unit
        # Regressions found by the v1 eval run:
        ("9.000", "MYR", "9.00"),  # SROIE unit price: MYR uses "." as the decimal point
        ("-.01", None, "-0.01"),  # leading dot used to become -1.00
        ("−0.01", None, "-0.01"),  # unicode minus sign
        ("–0.01", None, "-0.01"),  # en dash used as minus
    ],
)
def test_parse_amount(text, currency, expected):
    assert parse_amount(text, currency) == Decimal(expected)


@pytest.mark.parametrize("text", [None, "", "   ", "abc", "RM", "2 3.50", "1.23.4"])
def test_parse_amount_refuses_to_guess(text):
    assert parse_amount(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2", "2"), ("x2", "2"), ("0.500", "0.500"), ("1,5", "1.5"), ("0", None), ("1.2.3", None)],
)
def test_parse_quantity(text, expected):
    assert parse_quantity(text) == (Decimal(expected) if expected else None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("14/03/2018", date(2018, 3, 14)),
        ("14-03-18", date(2018, 3, 14)),
        ("14.03.2018", date(2018, 3, 14)),
        ("05/04/2018", date(2018, 4, 5)),  # day-first, not US month-first
        ("2018-04-05", date(2018, 4, 5)),  # ISO must not be swapped by dayfirst
        ("14 MAR 2018", date(2018, 3, 14)),
        ("MAR 14, 2018", date(2018, 3, 14)),
        ("Date: 14/03/2018 10:32:11 AM", date(2018, 3, 14)),
        ("12/25/2018", date(2018, 12, 25)),  # impossible as day-first, so month-first
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    "text", [None, "", "garbage", "14/03", "MAR 2018", "31/02/2018", "1/1/1850"]
)
def test_parse_date_refuses_partial_or_invalid(text):
    assert parse_date(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("RM", "MYR"),
        ("myr", "MYR"),
        ("Rp", "IDR"),
        ("Rp.", "IDR"),
        ("$", "USD"),
        ("XYZ", None),
        (None, None),
    ],
)
def test_parse_currency(text, expected):
    assert parse_currency(text) == expected


def test_vendor_key_ignores_suffix_case_and_punctuation():
    assert vendor_key("Maju Stationery Sdn. Bhd.") == vendor_key("MAJU STATIONERY SDN BHD")
    assert vendor_key("A & W") == "A AND W"


def test_normalize_record_myr(myr_raw):
    record, issues = normalize_record(myr_raw)
    assert issues == []
    assert record.currency == "MYR"
    assert record.date == date(2018, 3, 14)
    assert (record.subtotal, record.tax, record.total) == (
        Decimal("8.96"),
        Decimal("0.54"),
        Decimal("9.50"),
    )
    assert record.line_items[0].quantity == Decimal("2")


def test_normalize_record_idr_uses_home_currency_and_thousands(idr_raw):
    record, issues = normalize_record(idr_raw, default_currency="IDR")
    assert record.currency == "IDR"
    assert record.total == Decimal("38500.00")
    assert [i.code for i in issues] == [IssueCode.CURRENCY_INFERRED]
    assert not issues[0].blocking


def test_normalize_flags_printed_but_unreadable_values():
    raw = RawExtraction(document_type="receipt", vendor_name="X", date="sometime", total="lots")
    record, issues = normalize_record(raw)
    assert record.total is None and record.date is None
    assert {(i.code, i.field) for i in issues} == {
        (IssueCode.UNPARSEABLE_VALUE, "total"),
        (IssueCode.UNPARSEABLE_VALUE, "date"),
    }


def test_raw_extraction_treats_placeholder_strings_as_null():
    raw = RawExtraction(document_type="receipt", tax="N/A", subtotal="", invoice_number="null")
    assert raw.tax is None and raw.subtotal is None and raw.invoice_number is None


def test_raw_extraction_rejects_unknown_fields():
    with pytest.raises(ValueError):
        RawExtraction.model_validate({"document_type": "receipt", "status": "posted"})


def test_implausible_rounding_is_ignored_with_a_warning():
    # OCR read "-0.01" as "-001"; the model copied it faithfully.
    raw = RawExtraction(document_type="receipt", subtotal="80.91", rounding="-001", total="80.90")
    record, issues = normalize_record(raw)
    assert record.rounding is None
    assert [(i.code, i.blocking) for i in issues] == [(IssueCode.IMPLAUSIBLE_VALUE, False)]


def test_small_rounding_is_kept():
    raw = RawExtraction(document_type="receipt", subtotal="80.91", rounding="-0.01", total="80.90")
    record, issues = normalize_record(raw)
    assert record.rounding == Decimal("-0.01") and issues == []
