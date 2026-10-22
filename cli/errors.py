"""0.3.3 module name for ppro_bridge.errors (BridgeError). Importing it gives the package module itself,
so there is one BridgeError class however it is imported."""

import importlib
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# This checkout's package must win over any other ppro_bridge on sys.path: right after the script's folder.
sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p or os.curdir)) != os.path.normcase(_REPO)]
sys.path.insert(1, _REPO)

sys.modules[__name__] = importlib.import_module("ppro_bridge.errors")
