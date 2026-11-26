# Receipt & Invoice Extraction Agent

Extracts structured data (vendor, date, line items, tax, total) from receipt and invoice
photos and PDFs, validates it, flags doubtful records for human review, and posts clean
ones to a ledger. Users can correct fields in chat.

See [PLAN.md](PLAN.md) for the design and build status.

## Layout

```
core/         schema, normalisers, validation, corrections, PII masking, injection detection
agent/        agent loop, LLM adapter, sessions, tracing          (step 4)
mcp_server/   MCP tools + SQLite store                            (step 3)
app/          Gradio UI                                           (step 8)
evals/        eval cases, runner, results                         (steps 2, 5, 6)
tests/        pytest suite
```

## Run the tests

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Windows
# .venv/bin/python -m pip install -r requirements-dev.txt     # Linux/macOS
python -m pytest
ruff check . && ruff format --check .
```

## Secrets

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored. CI reads keys from
GitHub Secrets.
