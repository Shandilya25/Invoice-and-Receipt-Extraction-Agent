"""Tracing: one JSON line per step (LLM call, tool call, OCR...) with latency, tokens and cost.

Every line passes through PII masking before it is written. Traces go to
traces/trace.jsonl (override with TRACE_FILE) and to the standard logger.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from core.pii import PIIMaskingFilter, mask_pii

logger = logging.getLogger("receipt_agent")
logger.addFilter(PIIMaskingFilter())


def _trace_file() -> Path:
    return Path(os.getenv("TRACE_FILE", "traces/trace.jsonl"))


def log_step(step: str, **fields) -> dict:
    """Write one trace record. Returns it (handy for tests and eval reports)."""
    record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "step": step, **fields}
    line = mask_pii(json.dumps(record, default=str, ensure_ascii=False))
    path = _trace_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    logger.info(line)
    return record


@contextmanager
def timed(step: str, **fields):
    """Time a block and log it. Add details inside the block via the yielded dict:

    with timed("ocr", doc_id=...) as t:
        t["chars"] = len(text)
    """
    extra: dict = {}
    start = time.perf_counter()
    try:
        yield extra
    except Exception as e:
        extra["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        latency_ms = round((time.perf_counter() - start) * 1000)
        log_step(step, latency_ms=latency_ms, **fields, **extra)
