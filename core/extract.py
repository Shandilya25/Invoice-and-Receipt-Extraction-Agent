"""LLM extraction: OCR text -> RawExtraction (values copied exactly as printed).

Structured output is enforced in two layers: JSON mode on the API, then Pydantic
validation here. If validation fails, the error is sent back to the model and it gets
MAX_ATTEMPTS tries in total; after that the caller sends the document to review.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from core.injection import UNTRUSTED_TEXT_RULE, wrap_untrusted
from core.llm import chat
from core.schema import RawExtraction

MAX_ATTEMPTS = 3
MAX_OCR_CHARS = 12_000  # a receipt is ~1-3k chars; anything longer is cut (and noted)

SYSTEM_PROMPT = f"""You extract data from receipts and invoices for a finance team.
You receive the OCR text of ONE document. {UNTRUSTED_TEXT_RULE}

Return ONE JSON object with exactly these keys:
{{
  "document_type": "receipt" | "invoice" | "other",
  "vendor_name": str | null,      "vendor_address": str | null,
  "vendor_tax_id": str | null,    "invoice_number": str | null,
  "date": str | null,             "currency": str | null,
  "line_items": [{{"description": str, "quantity": str | null,
                  "unit_price": str | null, "amount": str | null}}],
  "subtotal": str | null, "tax": str | null, "service_charge": str | null,
  "discount": str | null, "rounding": str | null, "total": str | null,
  "payment_method": str | null
}}

Rules:
- Copy every value EXACTLY as printed: keep the number format ("60.000", "1,234.50") and the
  date format ("25/12/2018"). Do not convert, translate or calculate anything.
- Use null for anything that is not printed. Never guess a vendor, date or amount.
- The text comes from OCR, so labels may be misspelled ("SUS_TSTAL" = SUBTOTAL, "TOTA" =
  TOTAL). Read labels sensibly, but still copy the amounts as printed.
- document_type: "invoice" if it is titled invoice/tax invoice and has an invoice number,
  "receipt" for till receipts and cash bills, "other" for anything that is not a receipt or
  invoice (forms, letters, menus).
- vendor_name: the business that issued the document (usually at the top), not the customer.
- total: the final amount payable after tax, discounts and rounding. Not cash given or change.
- subtotal: the amount before tax as printed (e.g. "SUBTOTAL", "Total Excl GST").
- tax: the total tax amount printed (GST, SST, VAT, PB1).
- discount, service_charge, rounding: as printed, keep any minus sign.
- line_items: purchased items, each with the amount charged for that line. Include priced
  add-ons as their own lines. Do not include tax, subtotal, total, cash or change lines.
- currency: the symbol or code printed with the amounts ("RM", "Rp", "$"), else null.
"""


SELF_CHECK_PROMPT = """A first reading of this document failed the arithmetic checks below. Re-read
the document carefully and correct any value you misread (for example a price taken as a
quantity, or a tax or cash line taken as the total). If the printed numbers really are
inconsistent, copy them exactly as printed: do not invent numbers to make them add up.
Return the complete JSON object.
"""


class ExtractionError(RuntimeError):
    """The model did not return a valid extraction within MAX_ATTEMPTS."""


def extract_fields(ocr_text: str, session_id: str = "", feedback: str = "") -> RawExtraction:
    """`feedback` lists checks a previous reading failed (the pipeline's self-check)."""
    text = ocr_text[:MAX_OCR_CHARS]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": wrap_untrusted(text, source="ocr")},
    ]
    if feedback:
        messages.append(
            {
                "role": "user",
                "content": SELF_CHECK_PROMPT + wrap_untrusted(feedback, source="failed_checks"),
            }
        )
    last_error = ""
    for _ in range(MAX_ATTEMPTS):
        reply = chat(
            messages, step="llm_extract", json_mode=True, temperature=0, session_id=session_id
        )
        content = reply.content or ""
        try:
            return RawExtraction.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = _short_error(e)
            messages += [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": f"That JSON was invalid: {last_error}. "
                    "Return the corrected JSON object only.",
                },
            ]
    raise ExtractionError(f"No valid extraction after {MAX_ATTEMPTS} attempts: {last_error}")


def _short_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:5]
        )
    return str(e)[:200]
