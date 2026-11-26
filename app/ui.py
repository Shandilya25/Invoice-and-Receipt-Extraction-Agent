"""Gradio chat UI.

Run (from the project folder, with the virtual environment's Python):
    Windows:      .venv\\Scripts\\python -m app.ui
    Linux/macOS:  .venv/bin/python -m app.ui
then open http://127.0.0.1:7860

Upload a receipt/invoice (image or PDF) and chat with the agent. Each browser tab gets its
own session id (shown on the page); the agent keeps that conversation's history.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

from agent.agent import Agent
from agent.mcp_client import MCPToolClient
from core.store import Store

load_dotenv()
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads")).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
os.environ["UPLOAD_DIR"] = str(UPLOAD_DIR)  # the MCP server subprocess reads the same folder

store = Store(os.getenv("DB_PATH", "data/app.db"))
tools = MCPToolClient()  # starts `python -m mcp_server.server` and keeps it running
agent = Agent(tools, store)

LEDGER_HEADERS = ["Entry", "Doc", "Vendor", "Date", "Currency", "Total", "Posted at"]
REVIEW_HEADERS = ["Doc", "File", "Vendor", "Total", "Issues"]


def save_upload(path: str) -> str:
    """Copy an uploaded file into UPLOAD_DIR under a random, safe name."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(path).name)[-60:]
    name = f"{uuid.uuid4().hex[:8]}_{safe}"
    shutil.copyfile(path, UPLOAD_DIR / name)
    return name


def tables():
    ledger = [
        [
            e["entry_id"],
            e["doc_id"],
            e["vendor_key"],
            e["date"],
            e["currency"],
            e["total"],
            e["posted_at"],
        ]
        for e in store.ledger()
    ]
    review = []
    for d in store.review_queue():
        record = json.loads(d["record_json"] or "{}")
        codes = sorted({i["code"] for i in json.loads(d["issues_json"] or "[]")})
        review.append(
            [
                d["doc_id"],
                d["file_name"],
                record.get("vendor_name"),
                record.get("total"),
                ", ".join(codes),
            ]
        )
    return ledger, review


def respond(message: dict, history: list, session_id: str):
    files = [save_upload(f) for f in message.get("files", [])]
    text = (message.get("text") or "").strip()
    if files:
        uploads = "\n".join(f"[uploaded file: {name}]" for name in files)
        text = f"{uploads}\n{text or 'Please process it.'}"
    if not text:
        return None, history, *tables()
    try:
        answer = agent.reply(session_id, text)
    except Exception as e:  # noqa: BLE001 - show a safe message, details are in the traces
        answer = f"Sorry, something went wrong ({type(e).__name__}). Please try again."
    history = history + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": answer},
    ]
    return None, history, *tables()


with gr.Blocks(title="Receipt Agent") as demo:
    session = gr.State()
    gr.Markdown(
        "# Receipt & Invoice Agent\n"
        "Upload a receipt or invoice (photo or PDF). Clean records are posted to the ledger; "
        "doubtful ones go to the review queue. You can correct a field in chat, e.g. "
        '*"the total on DOC-1A2B3C4D is 89.50"*.'
    )
    session_label = gr.Markdown()
    with gr.Row():
        with gr.Column(scale=3):
            chat = gr.Chatbot(height=520, label="Chat")
            box = gr.MultimodalTextbox(
                file_types=["image", ".pdf"],
                file_count="multiple",
                placeholder="Upload a receipt or type a message...",
                show_label=False,
            )
        with gr.Column(scale=2):
            with gr.Tab("Ledger (posted)"):
                ledger_table = gr.Dataframe(headers=LEDGER_HEADERS, interactive=False)
            with gr.Tab("Review queue"):
                review_table = gr.Dataframe(headers=REVIEW_HEADERS, interactive=False)

    def start_session():
        sid = uuid.uuid4().hex[:12]
        return sid, f"Session id: `{sid}`", *tables()

    demo.load(start_session, None, [session, session_label, ledger_table, review_table])
    box.submit(respond, [box, chat, session], [box, chat, ledger_table, review_table])


if __name__ == "__main__":
    # Locally bind to 127.0.0.1 so the printed link opens in a browser. Docker / Hugging Face
    # set GRADIO_SERVER_NAME=0.0.0.0 to accept outside connections (http://0.0.0.0:7860 is
    # not a URL a browser can open).
    host = os.getenv("GRADIO_SERVER_NAME", "127.0.0.1")
    port = int(os.getenv("PORT", "7860"))
    print(f"Receipt agent ready: open http://127.0.0.1:{port} in your browser", flush=True)
    demo.launch(server_name=host, server_port=port)
