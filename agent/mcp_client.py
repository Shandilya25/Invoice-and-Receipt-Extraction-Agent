"""A small synchronous wrapper around the async MCP client.

The agent loop and Gradio are synchronous, while the MCP SDK is async. We keep ONE
long-lived MCP connection on a background event loop (so the OCR model in the server
process stays loaded) and expose plain `list_tools()` / `call_tool()` methods.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading

from mcp import Client, StdioServerParameters


def stdio_server() -> StdioServerParameters:
    """Launch `python -m mcp_server.server` as a subprocess with our environment."""
    return StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server.server"], env=dict(os.environ)
    )


class MCPToolClient:
    def __init__(self, server=None, connect_timeout: float = 120):
        """`server` is anything mcp.Client accepts: stdio params (default) or, in tests, an
        in-process MCPServer instance."""
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._client: Client | None = None
        self._task = asyncio.run_coroutine_threadsafe(
            self._hold_connection(server or stdio_server()), self._loop
        )
        if not self._ready.wait(connect_timeout):
            raise RuntimeError("MCP server did not start in time")
        if self._task.done():  # connection failed: surface the real error
            self._task.result()

    async def _hold_connection(self, server) -> None:
        self._stop = asyncio.Event()
        try:
            async with Client(server) as client:
                self._client = client
                self._ready.set()
                await self._stop.wait()
        finally:
            self._ready.set()

    def list_tools(self) -> list[dict]:
        """Tools in OpenAI function-calling format, for the LLM."""
        result = self._run(self._client.list_tools(), timeout=30)
        specs = []
        for tool in result.tools:
            schema = dict(tool.input_schema)
            # session_id is filled in by the agent, not chosen by the model
            schema["properties"] = {
                k: v for k, v in schema.get("properties", {}).items() if k != "session_id"
            }
            schema["required"] = [r for r in schema.get("required", []) if r != "session_id"]
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": schema,
                    },
                }
            )
        return specs

    def call_tool(self, name: str, arguments: dict, timeout: float = 120) -> str:
        """Call a tool and return its result as text (JSON). Errors come back as
        {"error": ...} so the model can read them and recover."""
        try:
            result = self._run(self._client.call_tool(name, arguments), timeout=timeout)
        except TimeoutError:
            return json.dumps({"error": f"Tool {name} timed out after {timeout:.0f}s"})
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        if result.is_error:
            return json.dumps({"error": text})
        if result.structured_content is not None:
            return json.dumps(result.structured_content, default=str)
        return text

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
            try:
                self._task.result(timeout=10)
            except Exception:  # noqa: BLE001 - best effort on shutdown
                pass

    def _run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)
