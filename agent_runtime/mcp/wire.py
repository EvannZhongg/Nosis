"""Framing for the private Runtime-to-MCP-service channel."""

import json
import sys


# Leave output budgeting to the owning Session, including results with media.
MESSAGE_LIMIT = sys.maxsize


def encode(message: dict) -> bytes:
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
