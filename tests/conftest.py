"""Shared fixtures. All vendors, phone numbers and IDs here are fictional."""

from datetime import date

import pytest

from core.schema import RawExtraction
from core.validate import DocumentContext, ValidationConfig

# Malaysian-style receipt (SROIE layout): GST 6% added on top of the subtotal.
MYR_RECEIPT_TEXT = """\
MAJU STATIONERY SDN BHD
NO 12, JALAN CONTOH 3, 40170 SHAH ALAM
Tel: 03-1234 5678
GST ID: 000123456789
TAX INVOICE
Date: 14/03/2018  10:32:11
Cashier: AMIRAH
------------------------------
PEN BLUE 0.5MM      2 x 1.50     3.00
A4 PAPER 80GSM      1 x 5.96     5.96
------------------------------
Total Excl GST:                  8.96
GST @6%:                         0.54
Total Incl GST:             RM   9.50
CASH                            10.00
CHANGE                           0.50
Thank you. Please come again.
"""

# Indonesian-style receipt (CORD layout): no currency printed, dot = thousands separator.
IDR_RECEIPT_TEXT = """\
WARUNG CONTOH
Jl. Contoh No. 1, Jakarta
14/03/2019 12:01
NASI GORENG SPECIAL     1    25.000
ES TEH MANIS            2     5.000    10.000
SUBTOTAL                     35.000
PB1 10%                       3.500
TOTAL                        38.500
CASH                         50.000
CHANGE                       11.500
"""


@pytest.fixture
def myr_raw() -> RawExtraction:
    return RawExtraction.model_validate(
        {
            "document_type": "receipt",
            "vendor_name": "MAJU STATIONERY SDN BHD",
            "vendor_address": "NO 12, JALAN CONTOH 3, 40170 SHAH ALAM",
            "vendor_tax_id": "000123456789",
            "date": "14/03/2018",
            "currency": "RM",
            "line_items": [
                {
                    "description": "PEN BLUE 0.5MM",
                    "quantity": "2",
                    "unit_price": "1.50",
                    "amount": "3.00",
                },
                {
                    "description": "A4 PAPER 80GSM",
                    "quantity": "1",
                    "unit_price": "5.96",
                    "amount": "5.96",
                },
            ],
            "subtotal": "8.96",
            "tax": "0.54",
            "total": "9.50",
            "payment_method": "CASH",
        }
    )


@pytest.fixture
def idr_raw() -> RawExtraction:
    return RawExtraction.model_validate(
        {
            "document_type": "receipt",
            "vendor_name": "WARUNG CONTOH",
            "date": "14/03/2019",
            "currency": None,
            "line_items": [
                {"description": "NASI GORENG SPECIAL", "quantity": "1", "amount": "25.000"},
                {
                    "description": "ES TEH MANIS",
                    "quantity": "2",
                    "unit_price": "5.000",
                    "amount": "10.000",
                },
            ],
            "subtotal": "35.000",
            "tax": "3.500",
            "total": "38.500",
        }
    )


@pytest.fixture
def myr_ctx() -> DocumentContext:
    return DocumentContext(ocr_text=MYR_RECEIPT_TEXT, as_of=date(2018, 6, 1))


@pytest.fixture
def no_age_limit() -> ValidationConfig:
    return ValidationConfig(max_age_days=None)
