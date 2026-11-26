"""The receipt workflow: read -> extract -> normalise -> validate -> post or review.

`ReceiptPipeline._decide` is the ONLY code path that writes to the ledger, and it only does
so when validation passes. The LLM never decides whether something is posted.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

from core.corrections import CorrectionError, apply_correction
from core.extract import ExtractionError, extract_fields
from core.llm import LLMUnavailable
from core.normalize import normalize_record, vendor_key
from core.ocr import OcrResult, read_document
from core.schema import DocumentType, Issue, IssueCode, RawExtraction, ReceiptRecord, RecordStatus
from core.store import Store
from core.tracing import log_step, timed
from core.validate import (
    DocumentContext,
    ValidationConfig,
    ValidationResult,
    validate_record,
)

Extractor = Callable[[str, str, str], RawExtraction]  # (ocr_text, session_id, feedback)
OcrReader = Callable[[Path], OcrResult]

# Failed checks that suggest the model misread a number (rather than a bad document).
SELF_CHECK_CODES = {
    IssueCode.LINE_MATH,
    IssueCode.LINE_SUM_MISMATCH,
    IssueCode.TOTAL_MISMATCH,
    IssueCode.BAD_AMOUNT,
    IssueCode.TAX_RATE_ODD,
}


def config_from_env() -> ValidationConfig:
    """MAX_RECEIPT_AGE_DAYS=0 disables the age check (used for the 2017-2019 eval datasets)."""
    days = int(os.getenv("MAX_RECEIPT_AGE_DAYS", "730"))
    return ValidationConfig(max_age_days=days or None)


class ReceiptPipeline:
    def __init__(
        self,
        store: Store,
        extractor: Extractor = extract_fields,
        ocr: OcrReader = read_document,
        config: ValidationConfig | None = None,
        home_currency: str | None = None,
    ):
        self.store = store
        self.extractor = extractor
        self.ocr = ocr
        self.config = config or ValidationConfig()
        self.home_currency = home_currency

    # ------------------------------------------------------------------ public API

    def process(self, path: Path, session_id: str = "", home_currency: str | None = None) -> dict:
        """Process one uploaded file and return its summary (status, record, issues)."""
        path = Path(path)
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        currency = home_currency or self.home_currency

        with timed("ocr", session_id=session_id, file=path.name) as t:
            ocr = self.ocr(path)
            t.update(
                chars=len(ocr.text),
                confidence=round(ocr.confidence, 3),
                blur=round(ocr.blur_score or 0),
                source=ocr.source,
            )

        record, prior = self._extract(ocr.text, currency, session_id)
        doc_id = self.store.add_document(
            session_id=session_id,
            file_name=path.name,
            file_hash=file_hash,
            ocr_text=ocr.text,
            ocr_confidence=ocr.confidence,
            blur_score=ocr.blur_score,
            home_currency=currency,
            verified_fields_json="[]",
        )
        result = self._validate(doc_id, record, prior, verified=set())
        if result.codes & SELF_CHECK_CODES:
            record, prior, result = self._self_check(
                doc_id, ocr.text, currency, session_id, record, prior, result
            )
        return self._save(doc_id, record, prior, set(), result)

    def correct(self, doc_id: str, field: str, value: str, session_id: str = "") -> dict:
        """Apply a user's correction, log it, and re-run validation (which may post the record)."""
        doc = self._get(doc_id)
        if doc["status"] == RecordStatus.POSTED:
            raise CorrectionError(f"{doc_id} is already posted and locked; it cannot be changed.")

        record = ReceiptRecord.model_validate_json(doc["record_json"])
        fix = apply_correction(record, field, value)
        self.store.add_correction(doc_id, fix.field, fix.old_value, fix.new_value, session_id)
        log_step("correction", session_id=session_id, doc_id=doc_id, field=fix.field)

        # Issues found while normalising the corrected field no longer apply.
        prior = [i for i in _load_issues(doc["prior_issues_json"]) if i.field != fix.field]
        verified = set(json.loads(doc["verified_fields_json"])) | {fix.field}
        result = self._validate(doc_id, fix.record, prior, verified)
        return self._save(doc_id, fix.record, prior, verified, result)

    def summary(self, doc_id: str) -> dict:
        doc = self._get(doc_id)
        record = json.loads(doc["record_json"])
        return {
            "doc_id": doc_id,
            "file_name": doc["file_name"],
            "status": doc["status"],
            "ledger_entry": self.store.ledger_entry(doc_id),
            "record": {k: v for k, v in record.items() if v not in (None, [], "")},
            "issues": [
                {
                    "code": i.code.value,
                    "field": i.field,
                    "message": i.message,
                    "blocking": i.blocking,
                }
                for i in _load_issues(doc["issues_json"])
            ],
            "corrections": len(self.store.corrections(doc_id)),
        }

    # ------------------------------------------------------------------ steps

    def _extract(self, text: str, currency: str | None, session_id: str, feedback: str = ""):
        if not text.strip():
            return _failed("No text could be read from the document.")
        try:
            with timed("extract", session_id=session_id, self_check=bool(feedback)):
                raw = self.extractor(text, session_id, feedback)
        except (ExtractionError, LLMUnavailable) as e:
            return _failed(f"Automatic extraction failed: {e}")
        return normalize_record(raw, currency)

    def _self_check(self, doc_id, text, currency, session_id, record, prior, result):
        """The numbers don't add up, so the model may have misread something (e.g. a price
        taken as a quantity). Ask it once to re-read, telling it which checks failed, and keep
        the new reading only if it has FEWER blocking issues. The grounding check means it
        can't 'fix' the maths by inventing numbers that aren't printed."""
        problems = "\n".join(i.message for i in result.issues if i.code in SELF_CHECK_CODES)
        new_record, new_prior = self._extract(text, currency, session_id, feedback=problems)
        new_result = self._validate(doc_id, new_record, new_prior, verified=set())
        improved = _blocking(new_result) < _blocking(result)
        log_step(
            "self_check",
            session_id=session_id,
            doc_id=doc_id,
            before=_blocking(result),
            after=_blocking(new_result),
            kept_new=improved,
        )
        return (new_record, new_prior, new_result) if improved else (record, prior, result)

    def _validate(
        self, doc_id: str, record: ReceiptRecord, prior: list[Issue], verified: set[str]
    ) -> ValidationResult:
        """Run all checks. No side effects."""
        doc = self._get(doc_id)
        date = record.date.isoformat() if record.date else None
        total = str(record.total) if record.total is not None else None
        context = DocumentContext(
            ocr_text=doc["ocr_text"],
            ocr_confidence=doc["ocr_confidence"],
            blur_score=doc["blur_score"],
            is_duplicate=self.store.is_duplicate(
                doc_id, doc["file_hash"], vendor_key(record.vendor_name), date, total
            ),
            verified_fields=frozenset(verified),
        )
        return validate_record(record, context, self.config, prior)

    def _save(
        self,
        doc_id: str,
        record: ReceiptRecord,
        prior: list[Issue],
        verified: set[str],
        result: ValidationResult,
    ) -> dict:
        """Store the outcome. The ONLY place that posts to the ledger, and only if result.ok."""
        date = record.date.isoformat() if record.date else None
        total = str(record.total) if record.total is not None else None
        status = RecordStatus.POSTED if result.ok else RecordStatus.NEEDS_REVIEW
        if result.ok:
            self.store.post_to_ledger(
                doc_id, vendor_key(record.vendor_name), date, record.currency, total
            )
        self.store.update_document(
            doc_id,
            status=status.value,
            record_json=record.model_dump_json(),
            issues_json=_dump_issues(result.issues),
            prior_issues_json=_dump_issues(prior),
            verified_fields_json=json.dumps(sorted(verified)),
        )
        log_step("decision", doc_id=doc_id, status=status.value, issues=sorted(result.codes))
        return self.summary(doc_id)

    def _get(self, doc_id: str) -> dict:
        doc = self.store.get_document(doc_id)
        if doc is None:
            raise ValueError(f"Unknown document id {doc_id!r}.")
        return doc


def _failed(message: str) -> tuple[ReceiptRecord, list[Issue]]:
    issue = Issue(code=IssueCode.EXTRACTION_FAILED, message=message)
    return ReceiptRecord(document_type=DocumentType.RECEIPT), [issue]


def _blocking(result: ValidationResult) -> int:
    return sum(i.blocking for i in result.issues)


def _dump_issues(issues: list[Issue]) -> str:
    return json.dumps([i.model_dump(mode="json") for i in issues])


def _load_issues(text: str | None) -> list[Issue]:
    return [Issue.model_validate(i) for i in json.loads(text or "[]")]
