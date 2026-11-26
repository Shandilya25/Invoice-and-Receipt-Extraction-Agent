"""MCP server exposing the receipt tools to the agent.

Run standalone (stdio transport):  python -m mcp_server.server

Security design: there is no "post" or "approve" tool. Posting only happens inside the
pipeline when validation passes, so a prompt-injected agent has nothing to call that
would push a bad record into the ledger. File access is limited to the upload folder.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from core.ocr import IMAGE_TYPES
from core.pipeline import ReceiptPipeline, config_from_env
from core.store import Store

ALLOWED_SUFFIXES = IMAGE_TYPES | {".pdf"}


def build_server(pipeline: ReceiptPipeline, upload_dir: Path) -> MCPServer:
    server = MCPServer(
        "receipt-tools",
        instructions="Tools to process receipts/invoices, inspect them and correct fields.",
        log_level="WARNING",  # our own traces go to traces/trace.jsonl
    )

    @server.tool()
    def process_document(
        file_name: str, session_id: str = "", home_currency: str | None = None
    ) -> dict:
        """Read an uploaded receipt or invoice (image or PDF), extract its fields and validate
        them. Clean records are posted to the ledger automatically; doubtful ones go to the
        review queue. Returns doc_id, status, the extracted record and any issues."""
        with _expected_errors():
            path = _uploaded_file(upload_dir, file_name)
            return pipeline.process(path, session_id=session_id, home_currency=home_currency)

    @server.tool()
    def get_document(doc_id: str) -> dict:
        """Return the current status, extracted record and issues of one document."""
        with _expected_errors():
            return pipeline.summary(doc_id)

    @server.tool()
    def list_documents(session_id: str = "") -> list[dict]:
        """List recent documents (newest first) with their status, vendor and total."""
        rows = pipeline.store.list_documents(session_id or None)
        return [pipeline.summary(r["doc_id"]) | {"issues": None} for r in rows]

    @server.tool()
    def correct_field(doc_id: str, field: str, value: str, session_id: str = "") -> dict:
        """Correct one field of a document that is not yet posted, e.g. field="total",
        value="89.50". Use field names like vendor_name, date, total, subtotal, tax,
        currency, invoice_number or line_items[0].amount. The record is re-validated and
        posted automatically if every check now passes."""
        with _expected_errors():
            return pipeline.correct(doc_id, field, value, session_id=session_id)

    return server


@contextmanager
def _expected_errors():
    """Our ValueErrors (bad file, rejected correction, unknown doc id) are messages for the
    model to read and act on. Anything else stays a crash, and the SDK hides its details."""
    try:
        yield
    except ValueError as e:
        raise ToolError(str(e)) from e


def _uploaded_file(upload_dir: Path, file_name: str) -> Path:
    # Path(...).name drops any directory part, so "../../.env" becomes ".env" -> rejected below.
    path = upload_dir / Path(file_name).name
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError(f"Unsupported file type: {path.suffix or 'none'}. Use an image or PDF.")
    if not path.is_file():
        raise ValueError(f"No uploaded file named {path.name!r}.")
    return path


def main() -> None:
    load_dotenv()
    store = Store(os.getenv("DB_PATH", "data/app.db"))
    pipeline = ReceiptPipeline(
        store,
        config=config_from_env(),
        home_currency=os.getenv("DEFAULT_CURRENCY") or None,
    )
    upload_dir = Path(os.getenv("UPLOAD_DIR", "uploads")).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    build_server(pipeline, upload_dir).run()  # stdio


if __name__ == "__main__":
    main()
