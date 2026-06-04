#!/usr/bin/env python
"""
myscoop.py — Entry point for the myscoop package manager.

Run:  python myscoop.py <command> [args]
      python myscoop.py install mysqlworkbench
      python myscoop.py list
      python myscoop.py search mysql
"""

import sys

from myscoop.cli import main

def _configure_stdio() -> None:
    """Avoid Windows console/codepage crashes when installer UI text is logged."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    _configure_stdio()
    main()
