# Plan: Receipt and invoice extraction agent

## Build status

| Step | What | Status |
|---|---|---|
| 1 | Schema, normalisers, validators, corrections, PII masking, injection detection + unit tests | **Done** (117 tests) |
| 2 | OCR + preprocessing; pick 20 eval images, make variants, write case JSON files | Next |
| 3 | MCP server + SQLite (documents, records, corrections, ledger, review queue, sessions) | |
| 4 | Agent loop, LLM adapter, session memory, guardrails, tracing | |
| 5 | v1 baseline eval run | |
| 6 | Improvements, then v2 run and before/after table | |
| 7 | Injection eval case, PII-masked traces, gitleaks | |
| 8 | Gradio UI, Dockerfile, CI, HF Spaces deploy, README with results + demo | |

---

## 1. The task

Finance uploads receipts and invoices as photos, scanned PDFs or digital PDFs. The agent:

1. **Ingests.** Stores the file, hashes it (for duplicate detection) and detects its type.
2. **Preprocesses.** Straightens rotated or skewed images and scores blur. Poor quality is a signal, not a failure.
3. **Reads the text.** A digital PDF uses its text layer. An image or scanned PDF goes through OCR, which keeps word positions and confidence.
4. **Extracts.** An LLM copies values *exactly as printed* into a strict schema (`RawExtraction`). Invalid output is retried.
5. **Normalises.** Code parses amounts, dates and currencies (`ReceiptRecord`) with locale-aware rules.
6. **Validates.** Deterministic rules check required fields, arithmetic, dates, grounding, duplicates and injection.
7. **Routes.** A record that passes every check is posted to the ledger. Anything else goes to the review queue with reasons.
8. **Chat.** The user can ask about documents and correct fields. A correction triggers re-validation, and a clean record gets posted.

**Core principle:** the LLM proposes values and code makes the decision. Posting is gated server-side.

## 2. Schema: what's mandatory

| Field | Required? |
|---|---|
| `vendor_name`, `date`, `total`, `currency`, `document_type` | **Yes** |
| `invoice_number` | Only when the document is an invoice |
| `line_items[]`, `subtotal`, `tax`, `service_charge`, `discount`, `rounding` | Optional, but must reconcile if present |
| `vendor_address`, `vendor_tax_id`, `payment_method` | Optional |

`tax = null` means no tax line was printed; `0` means it was printed as zero. A missing currency falls back to the home currency and raises a `CURRENCY_INFERRED` warning, which doesn't block posting.

## 3. Validation flags (all block posting except `CURRENCY_INFERRED`)

| Flag | Rule |
|---|---|
| `MISSING_FIELD` | A required field is missing |
| `UNPARSEABLE_VALUE` | A value was printed but can't be read |
| `BAD_AMOUNT` | Total ≤ 0 |
| `LINE_MATH` | qty × unit price ≠ amount |
| `LINE_SUM_MISMATCH` | Items match neither the subtotal nor the total in any known receipt layout |
| `TOTAL_MISMATCH` | subtotal + tax + adjustments ≠ total (tax-exclusive and tax-inclusive layouts both accepted) |
| `TAX_RATE_ODD` | Tax is negative or above 30% of the base |
| `BAD_DATE` / `STALE_DATE` | Date is in the future / older than `max_age_days` (disabled for the 2017–2019 datasets) |
| `BAD_CURRENCY` | Currency isn't supported |
| `UNGROUNDED_VALUE` | Total, subtotal, tax or date doesn't appear in the OCR text. Fields a human confirmed in chat are exempt. |
| `LOW_QUALITY` | OCR confidence or sharpness is below its threshold |
| `DUPLICATE` | Same file hash, or same vendor + date + total, already posted |
| `POSSIBLE_INJECTION` | The document or an extracted field contains instruction-like text |
| `NOT_A_RECEIPT` | `document_type = other` |

## 4. Datasets

No single dataset labels every field, so they are combined. Datasets are used only as eval ground truth; nothing is trained.

| Dataset | Labels | Use |
|---|---|---|
| SROIE (ICDAR 2019), ~1,000 Malaysian receipts | company, date, address, total | vendor / date / total accuracy |
| CORD v2 (`naver-clova-ix/cord-v2`), 1,000 Indonesian receipts | menu items, subtotal, tax, service, discount, total (no vendor or date) | line items, tax, arithmetic |
| FUNSD, 199 forms | question / answer entities | negative cases (`NOT_A_RECEIPT`) |
| Derived variants | Same labels as the source | blur, rotation, PDF wrapping |
| Synthetic | Hand-written labels | injection, broken arithmetic, duplicates |

Things that catch you out: CORD prints `60.000` for 60000 IDR. SROIE dates are day-first in mixed formats. SROIE vendor names need fuzzy matching. Some SROIE labels are wrong, so eyeball the images you pick. Check each dataset's licence before committing images.

## 5. Eval set (20 cases)

| # | Source | Expected |
|---|---|---|
| 1–6 | SROIE, clean | POSTED, fields match |
| 7–11 | CORD, clean | POSTED, fields match |
| 12–13 | Blurred copies | Correct values, or REVIEW with `LOW_QUALITY`. Never wrong values that get posted. |
| 14 | Rotated copy | Fields match |
| 15 | Case 1 uploaded again | REVIEW, `DUPLICATE` |
| 16 | PDF | POSTED |
| 17 | FUNSD form | REVIEW, `NOT_A_RECEIPT` |
| 18 | Receipt containing injected text | REVIEW, `POSSIBLE_INJECTION`, ledger untouched |
| 19 | Items don't add up | REVIEW, `LINE_SUM_MISMATCH` |
| 20 | Scripted chat correction | POSTED with the corrected value, plus an audit row |

## 6. Metric

- **Field accuracy:** correct fields ÷ labelled fields (normalised; vendor fuzzy match ≥ 0.9; line items scored with F1).
- **Routing accuracy:** correct POSTED / REVIEW decisions ÷ number of cases.
- **Unsafe posts:** records that should have gone to review but were posted. **Must be 0.**
- **Score** = 0.6 × field accuracy + 0.4 × routing accuracy. CI fails if the score is below 0.80 or there's any unsafe post.
- **Before and after:** v1 uses a naive prompt with no preprocessing. v2 adds deskew, locale-aware parsing, and one validation-feedback retry.

## 7. Correcting a field in chat

```
User:  Process receipt_017.jpg
Agent: MAJU STATIONERY SDN BHD · 2018-03-14 · subtotal 8.69 · tax 0.54 · total 9.50 MYR
       ⚠ Review: 8.69 + 0.54 = 9.23 ≠ 9.50 (TOTAL_MISMATCH). Items add up to 8.96.
User:  Subtotal is 8.96, OCR misread it.
Agent: [update_field → validate → post_to_ledger] Fixed 8.69 → 8.96. Posted as LEDGER-0042.
User:  What was the vendor on that one?
Agent: MAJU STATIONERY SDN BHD.
```

What happens: the agent resolves the target document from session state, parses the correction through the same normalisers (only allow-listed fields can be changed; status can't be), writes an audit row, marks the field as human-verified, re-validates the whole record, then re-routes it. Posted records are locked. There is no tool that skips validation.

## 8. Architecture

```
Gradio chat UI (session_id)
  └ Agent loop: LLM + tools · max 8 steps · timeouts · retries · tracing
      └ MCP server (FastMCP, stdio)
          ingest_document · ocr_document · extract_fields · validate_record
          update_field · post_to_ledger (re-validates; refuses if anything fails)
          flag_for_review · get_record · list_documents
          └ SQLite: documents, records, corrections, ledger, review_queue, sessions
```
