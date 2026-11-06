"""The MCP server over stdio, on the official MCP Python SDK (`mcp`, MIT; tested with 2.3.0).

  python -m ppro_bridge.mcp            serve on stdin/stdout (what an MCP client starts)

The SDK carries the protocol (handshake, version negotiation, JSON-RPC framing, errors); the tools are ours
(tools.py: generated from the CLI, the confirm gate, the meta tools), and so is the broker connection (connection.py:
one persistent connection, the CLI's hello and pairing token). Rules from SECURITY_REVIEW.md that hold here:
- stdio only: no listening socket.
- stdout carries protocol messages only. The real stdout is handed to the SDK; file descriptor 1 and sys.stdout then
  point at stderr, so nothing else (a print, a child process) can write into the protocol stream.
- tool calls run one at a time in a worker thread (one request at a time reaches Premiere anyway), sharing one
  connection, which is closed on exit.
- the pairing token is scrubbed from every result and log line (redact.py).
Logging goes to stderr; PPRO_MCP_LOG=DEBUG|INFO|WARNING (default WARNING).
"""

import io
import logging
import os
import sys
import threading

from . import connection, redact, tools

INVALID_PARAMS = -32602
SDK_HINT = ('The MCP server needs the MCP Python SDK, which the CLI does not: python -m pip install "mcp>=2.3,<3"')


def _protocol_stdout():
    """A text stream on the real stdout for the SDK, with fd 1 and sys.stdout moved to stderr."""
    sys.stdout.flush()
    keep = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return io.TextIOWrapper(os.fdopen(keep, "wb", buffering=0), encoding="utf-8", newline="\n",
                            line_buffering=True, write_through=True)


def build_server(rpc, all_tools=None):
    """The SDK Server with our two handlers. `rpc`: a Connection (or a test panel)."""
    import anyio
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.shared.exceptions import MCPError

    from .._version import VERSION

    gate = threading.Lock()  # one tool call at a time

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool.model_validate(d) for d in tools.list_tools(all_tools)])

    def run(name, arguments):
        with gate:
            return tools.call_tool(name, arguments, rpc, all_tools=all_tools)

    async def on_call_tool(ctx, params):
        try:
            result = await anyio.to_thread.run_sync(run, params.name, params.arguments)
        except tools.UnknownTool as e:
            raise MCPError(INVALID_PARAMS, redact.scrub(str(e))) from None
        return types.CallToolResult.model_validate(result)

    return Server("ppro-agent-bridge", version=VERSION, title="Premiere Pro Agent Bridge",
                  instructions=tools.instructions(all_tools), on_list_tools=on_list_tools,
                  on_call_tool=on_call_tool)


async def serve(rpc, all_tools=None, stdin=None, stdout=None):
    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server(rpc, all_tools)
    async with stdio_server(stdin=stdin, stdout=stdout) as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv=None) -> int:
    level = os.environ.get("PPRO_MCP_LOG", "WARNING").upper()
    root = logging.getLogger()
    root.handlers[:] = [redact.log_handler(sys.stderr)]
    root.setLevel(getattr(logging, level, logging.WARNING))
    try:
        import anyio
        import mcp  # noqa: F401
    except ImportError:
        sys.stderr.write(SDK_HINT + "\n")
        return 2
    out = anyio.wrap_file(_protocol_stdout())
    rpc = connection.Connection()
    try:
        anyio.run(serve, rpc, tools.list_all(), None, out)
    except KeyboardInterrupt:
        pass
    finally:
        close = getattr(rpc, "close", None)
        if close:
            close()
    return 0
