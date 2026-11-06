"""The MCP server of ppro-agent-bridge: the CLI's commands as MCP tools.

  tools.py       the tools, generated from the CLI's argparse definitions and actions.json: list_tools(),
                 call_tool(), instructions(); schemas, annotations, the confirm gate, the meta tools
  connection.py  one persistent broker connection per server process (transport.py's hello, token and replies)
  redact.py      keeps the pairing token out of every result, tool list and log line

None of these import the MCP SDK; the protocol layer that carries them is separate, so the CLI works without it.
"""
