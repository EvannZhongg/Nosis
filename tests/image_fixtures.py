"""Image byte samples shared by media, tool and interface tests."""

import struct
import zlib


def png_bytes(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg_bytes(width: int, height: int) -> bytes:
    # SOI, an APP0 segment, then the baseline start-of-frame that
    # carries the dimensions.
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 11)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"
