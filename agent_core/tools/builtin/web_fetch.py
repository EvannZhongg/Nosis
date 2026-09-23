"""Fetch public web pages without following SSRF-prone redirects blindly."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from urllib.parse import urljoin

import httpx

from ..base import JSONValue, Tool, ToolDefinition
from ..budget import MAX_TOOL_RESULT_CHARS, escaped_length, envelope_chars
from ..context import ToolExecutionContext
from ...public_url import require_public_url


MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0
USER_AGENT = "Nosis/1.0 web_fetch"
_RESULT_RESERVE_CHARS = 200
_CHUNK_SIZE = 64 * 1024


class WebFetchTool(Tool):
    name = "web_fetch"
    concurrent = True

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Fetch a public HTTP(S) URL and return its status, final URL, "
                "content type, and bounded text content. Redirects and DNS "
                "targets are checked to prevent access to private networks."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Public http(s) URL to read.",
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        url = arguments.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("web_fetch requires a non-empty string 'url'")
        if set(arguments) != {"url"}:
            raise ValueError("web_fetch accepts only 'url'")

        status, content_type, body, final_url, response_truncated = _fetch(url)
        text = _decode_body(body, content_type)
        if "html" in content_type.lower():
            text = _html_to_text(text)
        output = {
            "url": final_url,
            "status_code": status,
            "content_type": content_type,
            "content": text,
            "truncated": response_truncated,
        }
        return _fit_to_budget(output)


def _fetch(
    url: str,
) -> tuple[int, str, bytes, str, bool]:
    with httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
    ) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            require_public_url(current)
            with client.stream(
                "GET",
                current,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                location = response.headers.get("location")
                if 300 <= response.status_code < 400 and location:
                    current = urljoin(str(response.url), location)
                    continue
                body, truncated = _read_bounded(response)
                return (
                    response.status_code,
                    response.headers.get("content-type", ""),
                    body,
                    str(response.url),
                    truncated,
                )
        raise ValueError(
            f"web_fetch followed more than {MAX_REDIRECTS} redirects from "
            f"'{url}'"
        )


def _read_bounded(response) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        remaining = MAX_RESPONSE_BYTES - size
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
        if size == MAX_RESPONSE_BYTES:
            return b"".join(chunks), True
    return b"".join(chunks), False


def _decode_body(body: bytes, content_type: str) -> str:
    match = re.search(
        r"(?:^|;)\s*charset=\s*[\"']?([^;\"'\s]+)",
        content_type,
        re.I,
    )
    encoding = match.group(1) if match else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _html_to_text(value: str) -> str:
    value = re.sub(
        r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>",
        "",
        value,
        flags=re.I | re.S,
    )
    value = re.sub(
        r"<\s*/?\s*(?:a|h[1-6]|li|p|br)\b[^>]*>",
        "\n",
        value,
        flags=re.I,
    )
    value = re.sub(r"<[^>]*>", "", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return re.sub(r"\n{2,}", "\n", value).strip()


def _fit_to_budget(output: dict[str, JSONValue]) -> dict[str, JSONValue]:
    content = output["content"]
    if not isinstance(content, str):
        return output

    skeleton = dict(output)
    skeleton["content"] = ""
    skeleton["truncated"] = True
    available = (
        MAX_TOOL_RESULT_CHARS
        - envelope_chars(skeleton)
        - _RESULT_RESERVE_CHARS
    )
    if available < 0:
        raise ValueError("web_fetch result metadata exceeds the tool budget")

    kept: list[str] = []
    used = 0
    for chunk in _chunks(content):
        for character in chunk:
            cost = escaped_length(character)
            if used + cost > available:
                output["content"] = "".join(kept)
                output["truncated"] = True
                return output
            kept.append(character)
            used += cost
    output["content"] = "".join(kept)
    return output


def _chunks(value: str, size: int = _CHUNK_SIZE) -> Iterable[str]:
    for start in range(0, len(value), size):
        yield value[start : start + size]
