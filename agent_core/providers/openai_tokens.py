"""Local request estimates using bundled OpenAI vocabularies, without downloads."""

import base64
import gzip
import json
from functools import lru_cache
from importlib.resources import files
from threading import Lock

import tiktoken


_encoding_lock = Lock()


@lru_cache(maxsize=2)
def _encoding(name: str) -> tiktoken.Encoding:
    resources = files(__package__).joinpath("data", "tokenizers")
    definition = json.loads(resources.joinpath("encodings.json").read_text(encoding="utf-8"))[name]
    vocabulary = gzip.decompress(resources.joinpath(name + ".tiktoken.gz").read_bytes())
    ranks = {
        base64.b64decode(token): int(rank)
        for token, rank in (line.split() for line in vocabulary.splitlines())
    }
    return tiktoken.Encoding(**definition, mergeable_ranks=ranks)


class TokenCounter:
    """Estimate each request with a reusable encoding and no content cache."""

    def __init__(self, model: str) -> None:
        try:
            name = tiktoken.encoding_name_for_model(model.removeprefix("openai/"))
        except KeyError:
            name = "cl100k_base"
        self.identity = [name, tiktoken.__version__]
        # Concurrent sessions must not build the same vocabulary in parallel.
        with _encoding_lock:
            self._encoding = _encoding(name)

    def _text(self, text: str) -> int:
        return len(self._encoding.encode_ordinary(text))

    def count(self, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> int:
        total = 3
        for message in messages:
            total += 3
            for field, value in message.items():
                if isinstance(value, str):
                    total += self._text(value)
                elif field == "content" and isinstance(value, list):
                    total += sum(self._text(part["text"]) for part in value if part["type"] == "text")
                elif field == "tool_calls":
                    total += sum(self._text(call["function"]["arguments"])
                                 + self._text(call["function"]["name"]) for call in value)
        if tools:
            total += self._text(json.dumps(tools, ensure_ascii=False)) + 9
        return total
