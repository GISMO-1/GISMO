"""GISMO desktop launcher — double-click to open (no terminal window on Windows).

Uses pythonw.exe on Windows so no console appears. Equivalent to `gismo app`.
"""
import os
import sys

# Ensure the repo root is importable when launched by double-click
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from gismo.desktop.app import launch

_DB_PATH = os.path.join(_HERE, ".gismo", "state.db")
launch(_DB_PATH)
