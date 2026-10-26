"""Local request estimates using bundled OpenAI vocabularies, without downloads."""

import base64
import gzip
import json
from functools import lru_cache
from importlib.resources import files

import tiktoken


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
    """Cache only the latest request's rendered messages and tool schema.

    Keys use content rather than object identity because tool parameters and
    provider metadata can be mutated even on frozen message objects.
    """

    def __init__(self, model: str) -> None:
        try:
            name = tiktoken.encoding_name_for_model(model.removeprefix("openai/"))
        except KeyError:
            name = "cl100k_base"
        self.identity = [name, tiktoken.__version__]
        self._encoding = _encoding(name)
        self._messages: dict[str, int] = {}
        self._tools: tuple[str, int] = ("[]", 0)

    def _text(self, text: str) -> int:
        return len(self._encoding.encode_ordinary(text))

    def count(self, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> int:
        current: dict[str, int] = {}
        total = 3
        for message in messages:
            key = json.dumps(message, ensure_ascii=False)
            tokens = self._messages.get(key)
            if tokens is None:
                tokens = 3
                for field, value in message.items():
                    if isinstance(value, str):
                        tokens += self._text(value)
                    elif field == "content" and isinstance(value, list):
                        tokens += sum(self._text(part["text"]) for part in value if part["type"] == "text")
                    elif field == "tool_calls":
                        tokens += sum(self._text(call["function"]["arguments"])
                                      + self._text(call["function"]["name"]) for call in value)
            current[key] = tokens
            total += tokens
        schema = json.dumps(tools, ensure_ascii=False)
        previous_schema, tools_tokens = self._tools
        if schema != previous_schema:
            tools_tokens = self._text(schema) + 9 if tools else 0
            self._tools = (schema, tools_tokens)
        self._messages = current
        return total + tools_tokens
