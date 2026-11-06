"""python -m ppro_bridge.mcp: the MCP server over stdio (see server.py)."""
import sys

from .server import main

sys.exit(main())
