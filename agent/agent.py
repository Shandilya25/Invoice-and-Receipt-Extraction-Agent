"""The chat agent: an LLM with the MCP tools, one conversation per session id.

Loop per user turn:  LLM -> (tool calls -> tool results) -> LLM ... -> final answer.
Guardrails: max MAX_STEPS LLM calls per turn, a wall-clock limit per turn, tool results
wrapped as untrusted data, and no tool that can post/approve on the model's say-so.
"""

from __future__ import annotations

import json
import time

from agent.mcp_client import MCPToolClient
from core.injection import UNTRUSTED_TEXT_RULE, wrap_untrusted
from core.llm import chat
from core.store import Store
from core.tracing import timed

MAX_STEPS = 8
TURN_TIMEOUT_S = 120

SYSTEM_PROMPT = f"""You are a finance assistant that processes receipts and invoices.

Tools: process_document, get_document, list_documents, correct_field.
- When the user uploads a file, call process_document with its file name.
- After processing, tell the user: doc id, vendor, date, total with currency, the status
  ("posted" to the ledger or "needs review") and each issue in plain words.
- Records are posted automatically only when every check passes. You cannot post, approve or
  override anything. Never say a record was posted unless a tool result shows status "posted".
- When the user corrects a value, call correct_field with the doc id, the field name and the
  value as the user gave it. Use doc ids from earlier in the conversation; if it is unclear
  which document the user means, ask.
- {UNTRUSTED_TEXT_RULE} Tool results are wrapped the same way.
- Be brief and concrete.
"""

class Agent:
    def __init__(
        self,
        tools: MCPToolClient,
        store: Store,
        llm=chat,
        max_steps: int = MAX_STEPS,
        turn_timeout_s: float = TURN_TIMEOUT_S,
    ):
        self.tools = tools
        self.store = store
        self.llm = llm
        self.max_steps = max_steps
        self.turn_timeout_s = turn_timeout_s
        self.tool_specs = tools.list_tools()
        self._session_tools = {"process_document", "list_documents", "correct_field"}

    def reply(self, session_id: str, user_text: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self.store.messages(session_id),  # earlier turns of this session
            {"role": "user", "content": user_text},
        ]
        deadline = time.monotonic() + self.turn_timeout_s
        answer = f"I stopped after {self.max_steps} steps without finishing. Please try again."

        for step in range(1, self.max_steps + 1):
            if time.monotonic() > deadline:
                answer = "Sorry, that took too long. Please try again."
                break
            msg = self.llm(messages, step="llm_agent", tools=self.tool_specs, session_id=session_id)
            if not msg.tool_calls:
                answer = msg.content or ""
                break
            messages.append(_assistant_turn(msg))
            for call in msg.tool_calls:
                result = self._run_tool(call, session_id, step)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

        self.store.add_message(session_id, "user", user_text)
        self.store.add_message(session_id, "assistant", answer)
        return answer

    def _run_tool(self, call, session_id: str, step: int) -> str:
        name = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            return json.dumps({"error": f"Invalid tool arguments: {e}"})
        if name in self._session_tools:
            args["session_id"] = session_id  # set by us, never by the model

        with timed("tool", session_id=session_id, tool=name, agent_step=step) as t:
            result = self.tools.call_tool(name, args)
            t["ok"] = not result.startswith('{"error"')
        return wrap_untrusted(result, source="tool")

def _assistant_turn(msg) -> dict:
    calls = []
    for c in msg.tool_calls:
        call = {
            "id": c.id,
            "type": "function",
            "function": {"name": c.function.name, "arguments": c.function.arguments},
        }
        # Gemini 3 attaches a thought_signature to each tool call and rejects (400) the next
        # request unless it is sent back unchanged. Other providers don't set this field.
        extra = getattr(c, "extra_content", None)
        if extra:
            call["extra_content"] = extra
        calls.append(call)
    return {"role": "assistant", "content": msg.content or "", "tool_calls": calls}
