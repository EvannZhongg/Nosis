"""Reuse immutable TLS trust configuration across request-owned HTTP clients."""

from functools import lru_cache
import os
from pathlib import Path
import ssl
from threading import Lock

import httpx


_context_lock = Lock()


@lru_cache(maxsize=4)
def _context(cafile: str | None, capath: str | None, revision: tuple | None) -> ssl.SSLContext:
    if cafile or capath:
        return ssl.create_default_context(cafile=cafile, capath=capath)
    return httpx.create_ssl_context(trust_env=False)


def shared_ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE")
    capath = None if cafile else os.environ.get("SSL_CERT_DIR")
    path = cafile or capath
    stat = Path(path).stat() if path else None
    revision = (stat.st_mtime_ns, stat.st_size) if stat else None
    with _context_lock:
        return _context(cafile, capath, revision)
