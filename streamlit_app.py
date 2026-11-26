"""Streamlit chat UI. This is also the entry point Streamlit Community Cloud runs.

    streamlit run streamlit_app.py      ->  http://localhost:8501

Upload a receipt/invoice (image or PDF) and chat with the agent. Each browser session gets its
own session id (shown on the page); the agent keeps that conversation's history.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()  # local runs: keys come from .env
try:  # Streamlit Cloud: keys come from the app's Secrets settings
    for _key, _value in st.secrets.items():
        if isinstance(_value, str):
            os.environ.setdefault(_key, _value)
except Exception:  # noqa: BLE001 - no secrets file locally, .env is used instead
    pass

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads")).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
os.environ["UPLOAD_DIR"] = str(UPLOAD_DIR)  # the MCP server subprocess reads the same folder
SAMPLES = Path(__file__).parent / "samples"
FILE_TYPES = ["jpg", "jpeg", "png", "webp", "pdf"]

from agent.agent import Agent  # noqa: E402 - imported after the environment is set up
from agent.mcp_client import MCPToolClient  # noqa: E402
from core.store import Store  # noqa: E402


@st.cache_resource(show_spinner="Starting the agent and its MCP server...")
def backend() -> tuple[Store, Agent]:
    """One agent + one MCP server process, shared by all browser sessions."""
    store = Store(os.getenv("DB_PATH", "data/app.db"))
    return store, Agent(MCPToolClient(), store)


def save_upload(name: str, data: bytes) -> str:
    """Store an uploaded file in UPLOAD_DIR under a random, safe name."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)[-60:]
    stored = f"{uuid.uuid4().hex[:8]}_{safe}"
    (UPLOAD_DIR / stored).write_bytes(data)
    return stored


def send(text: str, files: list[tuple[str, bytes]]) -> None:
    names = [save_upload(name, data) for name, data in files]
    if names:
        uploads = "\n".join(f"[uploaded file: {n}]" for n in names)
        text = f"{uploads}\n{text or 'Please process it.'}"
    if not text.strip():
        return
    st.session_state.chat.append({"role": "user", "content": text})
    with st.spinner("Reading the document and checking it (usually 5-20 s)..."):
        try:
            answer = agent.reply(st.session_state.session_id, text)
        except Exception as e:  # noqa: BLE001 - safe message; details are in the traces
            answer = f"Sorry, something went wrong ({type(e).__name__}). Please try again."
    st.session_state.chat.append({"role": "assistant", "content": answer})


def ledger_rows(store: Store) -> list[dict]:
    return [
        {
            "Entry": e["entry_id"],
            "Doc": e["doc_id"],
            "Vendor": e["vendor_key"],
            "Date": e["date"],
            "Currency": e["currency"],
            "Total": e["total"],
        }
        for e in store.ledger()
    ]


def review_rows(store: Store) -> list[dict]:
    rows = []
    for d in store.review_queue():
        record = json.loads(d["record_json"] or "{}")
        codes = sorted({i["code"] for i in json.loads(d["issues_json"] or "[]")})
        rows.append(
            {
                "Doc": d["doc_id"],
                "Vendor": record.get("vendor_name"),
                "Total": record.get("total"),
                "Issues": ", ".join(codes),
            }
        )
    return rows


# --------------------------------------------------------------------------- page

st.set_page_config(page_title="Receipt Agent", page_icon="🧾", layout="wide")
store, agent = backend()
if "session_id" not in st.session_state:
    st.session_state.session_id = uuid.uuid4().hex[:12]
    st.session_state.chat = []

st.title("🧾 Receipt & Invoice Agent")
st.caption(
    "Upload a receipt or invoice (photo or PDF). Clean records are posted to the ledger; "
    "doubtful ones go to the review queue. You can correct a field in chat, e.g. "
    '*"the vendor is Kedai Kopi Maju"*.  \n'
    f"Session id: `{st.session_state.session_id}`"
)

left, right = st.columns([3, 2], gap="large")
with left:
    st.write("**Try a sample:**")
    samples = sorted(SAMPLES.iterdir())
    labels = ["1 · Clean receipt", "2 · No vendor name", "3 · Prompt injection", "4 · Invoice PDF"]
    for column, path, label in zip(st.columns(len(samples)), samples, labels, strict=False):
        if column.button(label, width="stretch"):
            send("Please process this document.", [(path.name, path.read_bytes())])
            st.rerun()
    st.caption('For sample 2, then type: *"The vendor is Kedai Kopi Maju"*')

    for message in st.session_state.chat:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

with right:
    ledger_tab, review_tab = st.tabs(["Ledger (posted)", "Review queue"])
    with ledger_tab:
        st.dataframe(ledger_rows(store), hide_index=True, width="stretch")
    with review_tab:
        st.dataframe(review_rows(store), hide_index=True, width="stretch")

prompt = st.chat_input(
    "Upload a receipt or type a message...", accept_file="multiple", file_type=FILE_TYPES
)
if prompt:
    send(prompt.text, [(f.name, f.getvalue()) for f in prompt.files])
    st.rerun()
