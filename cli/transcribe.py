#!/usr/bin/env python3
"""transcribe — local Whisper transcription to Adobe transcript JSON (code: ppro_bridge.transcribe).

  python transcribe.py video.mp4 --output tr.json [--language en] [--model large-v3] [--digest]

Run as a script it is ppro_bridge.transcribe.main(); imported, it is the package module itself."""

import importlib
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# This checkout's package must win over any other ppro_bridge on sys.path: right after the script's folder.
sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p or os.curdir)) != os.path.normcase(_REPO)]
sys.path.insert(1, _REPO)

if __name__ == "__main__":
    from ppro_bridge import transcribe as _impl

    sys.exit(_impl.main())
else:
    sys.modules[__name__] = importlib.import_module("ppro_bridge.transcribe")
