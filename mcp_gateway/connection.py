"""A synchronous handle on a long-lived MCP session.

MCP is async and LangGraph's tool nodes here are sync, so something has to
bridge the two. The naive bridge -- `asyncio.run()` per tool call -- would
open a transport, initialize, call, and tear down every single time, which
for a stdio server means spawning a subprocess per tool call. This keeps one
session alive on a background event loop instead and submits calls to it.

Two transports, chosen by what you point it at:

  * stdio -- a locally-launched server process (`razorpay_mcp`, or Razorpay's
    own `razorpay/mcp` Docker image).
  * streamable HTTP -- a remote server such as `https://mcp.razorpay.com/mcp`.
    Note that Razorpay's hosted endpoint requires an interactive OAuth
    authorization; API-key Bearer tokens are rejected with OAUTH_BAD_TOKEN.
    Pass an already-obtained token via `auth_header` once you have one.

Nothing above this module knows or cares which transport is in use.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class MCPConnectionError(Exception):
    pass


class MCPToolError(MCPConnectionError):
    """The MCP call completed -- the transport worked -- but the tool itself
    reported failure via the protocol's own `CallToolResult.isError` field
    (Razorpay rejecting an amount, an unknown payment_link_id, and similar
    ordinary business-logic errors, as opposed to a dead session or a
    timeout). Callers that reserve budget before a call need this
    distinction: a `MCPToolError` means nothing was actually accomplished
    and any reservation must be released, exactly like a transport failure
    -- but unlike one, it is an expected, routine outcome, not a sign the
    session itself is broken."""


class MCPConnection:
    """One MCP session, owned by a background thread, callable synchronously."""

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        url: str | None = None,
        auth_header: str | None = None,
        timeout: float = 60.0,
    ):
        if not command and not url:
            raise ValueError("MCPConnection needs either a command (stdio) or a url (http)")
        self._command = command
        self._args = args or []
        self._env = env
        self._cwd = cwd
        self._url = url
        self._auth_header = auth_header
        self._timeout = timeout

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._tools: list[dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="mcp-connection", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=self._timeout):
            raise MCPConnectionError("timed out starting the MCP session")
        if self._error:
            raise MCPConnectionError(f"could not start the MCP session: {self._error}")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as e:  # noqa: BLE001 -- surfaced to start()
            self._error = e
            self._ready.set()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _serve(self) -> None:
        try:
            if self._url:
                from mcp.client.streamable_http import streamablehttp_client

                headers = {"Authorization": self._auth_header} if self._auth_header else None
                async with streamablehttp_client(self._url, headers=headers) as (read, write, _):
                    await self._session_loop(read, write)
            else:
                params = StdioServerParameters(
                    command=self._command, args=self._args, env=self._env, cwd=self._cwd
                )
                async with stdio_client(params) as (read, write):
                    await self._session_loop(read, write)
        except BaseException as e:  # noqa: BLE001
            self._error = e
            self._ready.set()
            raise

    async def _session_loop(self, read, write) -> None:
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            self._tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
                }
                for t in listing.tools
            ]
            self._session = session
            self._ready.set()
            # Hold the transport open until stop() is called. The session is
            # driven from other tasks via run_coroutine_threadsafe.
            while not self._stop.is_set():
                await asyncio.sleep(0.1)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._thread = None
        self._session = None

    # -- use ---------------------------------------------------------------

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    def call(self, name: str, args: dict[str, Any]) -> str:
        """Invoke an MCP tool and return its text content.

        Raises MCPToolError if the server marked its own result isError --
        found live, the hard way: a server can return a perfectly normal
        TextContent block describing a failure (Razorpay's real API
        rejecting an amount) without raising anything at the transport
        level at all. Silently returning that text as if it were a success
        is indistinguishable from an actual success to any caller that
        reserved budget before making this call -- checked here, once, so
        every caller of this method gets the distinction for free rather
        than having to parse tool-specific error shapes out of plain text.
        """
        if self._session is None or self._loop is None:
            raise MCPConnectionError("session is not running; call start() first")

        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, args), self._loop
        )
        result = future.result(timeout=self._timeout)

        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
        text = "\n".join(parts) if parts else ""

        if getattr(result, "isError", False):
            raise MCPToolError(text or f"{name} failed with no error detail")
        return text

    def __enter__(self) -> "MCPConnection":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
