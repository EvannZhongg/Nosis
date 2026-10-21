#!/usr/bin/env python3
"""Standalone Nosis hook entrypoint; no Nosis imports or external dependencies."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hookify_rules import evaluate

if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(evaluate(json.load(sys.stdin), Path.cwd()), ensure_ascii=False))
