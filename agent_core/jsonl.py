"""Append to logs after an interrupted final record."""

import json
import os
from typing import BinaryIO


def prepare_append(file: BinaryIO) -> None:
    end = file.seek(0, os.SEEK_END)
    if not end:
        return
    file.seek(end - 1)
    if file.read(1) == b"\n":
        return

    cursor = end
    start = 0
    while cursor:
        chunk_start = max(0, cursor - 4096)
        file.seek(chunk_start)
        chunk = file.read(cursor - chunk_start)
        newline = chunk.rfind(b"\n")
        if newline != -1:
            start = chunk_start + newline + 1
            break
        cursor = chunk_start

    file.seek(start)
    tail = file.read(end - start)
    try:
        json.loads(tail)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Only an unfinished final record can be discarded during replay.
        file.truncate(start)
    else:
        file.write(b"\n")
    file.seek(0, os.SEEK_END)
