#!/usr/bin/env python3
"""transcript_digest — Adobe transcript JSON to a sentence list with timecodes (code: ppro_bridge.digest).

  python transcript_digest.py tr.json [--json] [--min-conf 0.8]

Run as a script it is ppro_bridge.digest.main(); imported, it is the package module itself."""

import os
import sys

# Run as a script, Python puts this file's folder first on sys.path: a socket.py or json.py planted there would shadow
# the standard library before any code of ours runs (RA-3). Nothing is imported from this folder, so it is dropped
# (os and sys are loaded before any script runs; -P / -I leave the folder off already).
if __name__ == "__main__" and not getattr(sys.flags, "safe_path", False) and sys.path and \
        os.path.normcase(os.path.abspath(sys.path[0] or os.curdir)) == \
        os.path.normcase(os.path.dirname(os.path.abspath(__file__))):
    del sys.path[0]

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# This checkout's package must win over any other ppro_bridge on sys.path: first when run as a script (its own folder
# dropped above), else right after the importer's folder.
sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p or os.curdir)) != os.path.normcase(_REPO)]
sys.path.insert(0 if __name__ == "__main__" else 1, _REPO)

import importlib  # noqa: E402

if __name__ == "__main__":
    from ppro_bridge import digest as _impl

    sys.exit(_impl.main())
else:
    sys.modules[__name__] = importlib.import_module("ppro_bridge.digest")
