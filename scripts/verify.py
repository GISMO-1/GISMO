#!/usr/bin/env python3
"""Verification entrypoint for GISMO."""
from __future__ import annotations

import subprocess
import sys


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> int:
    print(f"Python version: {sys.version}")
    run([sys.executable, "-m", "unittest", "tests.test_smoke", "-v"])
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test*.py", "-v"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
