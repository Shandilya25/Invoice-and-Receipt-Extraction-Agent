"""SQLite persistence: documents, corrections (audit trail), ledger, chat messages.

The "ledger" table stands in for the finance system that clean records are posted to.
One small class, plain SQL, no ORM.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    session_id TEXT,
    file_name TEXT,
    file_hash TEXT,
    status TEXT,                 -- posted | needs_review
    record_json TEXT,            -- normalised ReceiptRecord
    issues_json TEXT,            -- issues from the latest validation
    prior_issues_json TEXT,      -- issues found while normalising (kept for re-validation)
    verified_fields_json TEXT,   -- fields a human confirmed in chat
    ocr_text TEXT,
    ocr_confidence REAL,
    blur_score REAL,
    home_currency TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT, field TEXT, old_value TEXT, new_value TEXT, session_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    entry_id TEXT PRIMARY KEY,
    doc_id TEXT UNIQUE,
    vendor_key TEXT, date TEXT, currency TEXT, total TEXT, posted_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, role TEXT, content TEXT, created_at TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = "data/app.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    # ------------------------------------------------------------------ documents

    def add_document(self, **fields) -> str:
        doc_id = "DOC-" + uuid.uuid4().hex[:8].upper()
        fields |= {"doc_id": doc_id, "created_at": _now(), "updated_at": _now()}
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.db:
            self.db.execute(
                f"INSERT INTO documents ({cols}) VALUES ({marks})", list(fields.values())
            )
        return doc_id

    def update_document(self, doc_id: str, **fields) -> None:
        fields["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE documents SET {sets} WHERE doc_id = ?", [*fields.values(), doc_id]
            )

    def get_document(self, doc_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def list_documents(self, session_id: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT doc_id, file_name, status, record_json, created_at FROM documents"
        args: list = []
        if session_id:
            sql += " WHERE session_id = ?"
            args.append(session_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        return [dict(r) for r in self.db.execute(sql, [*args, limit])]

    # ------------------------------------------------------------------ duplicates & ledger

    def is_duplicate(
        self, doc_id: str, file_hash: str, vendor_key: str, date: str | None, total: str | None
    ) -> bool:
        """Same file seen before, or the same vendor/date/total already posted."""
        same_file = self.db.execute(
            "SELECT 1 FROM documents WHERE file_hash = ? AND doc_id != ? LIMIT 1",
            (file_hash, doc_id),
        ).fetchone()
        same_entry = (
            vendor_key
            and date
            and total
            and self.db.execute(
                "SELECT 1 FROM ledger WHERE vendor_key = ? AND date = ? AND total = ?"
                " AND doc_id != ?",
                (vendor_key, date, total, doc_id),
            ).fetchone()
        )
        return bool(same_file or same_entry)

    def post_to_ledger(
        self, doc_id: str, vendor_key: str, date: str, currency: str, total: str
    ) -> str:
        entry_id = "LEDGER-" + uuid.uuid4().hex[:6].upper()
        with self.db:
            self.db.execute(
                "INSERT INTO ledger VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry_id, doc_id, vendor_key, date, currency, total, _now()),
            )
        return entry_id

    def ledger_entry(self, doc_id: str) -> str | None:
        row = self.db.execute("SELECT entry_id FROM ledger WHERE doc_id = ?", (doc_id,)).fetchone()
        return row["entry_id"] if row else None

    def ledger(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM ledger ORDER BY posted_at DESC")]

    def review_queue(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT doc_id, file_name, record_json, issues_json, updated_at FROM documents "
            "WHERE status = 'needs_review' ORDER BY updated_at DESC"
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ audit trail

    def add_correction(
        self, doc_id: str, field: str, old_value, new_value, session_id: str
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO corrections (doc_id, field, old_value, new_value, session_id, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    doc_id,
                    field,
                    json.dumps(old_value, default=str),
                    json.dumps(new_value, default=str),
                    session_id,
                    _now(),
                ),
            )

    def corrections(self, doc_id: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM corrections WHERE doc_id = ? ORDER BY id", (doc_id,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ chat history

    def add_message(self, session_id: str, role: str, content: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, _now()),
            )

    def messages(self, session_id: str, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
