"""Provider-independent token usage and its persisted representation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class InputTokenAnchor:
    fingerprint: str
    local_tokens: int
    input_tokens: int


@dataclass(frozen=True)
class TokenUsage:
    # Input totals include cache reads and writes; cache fields are breakdowns.
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    # None means unreported; zero means the provider reported no cache activity.
    cache_read_tokens: int | None
    cache_write_tokens: int | None


@dataclass(frozen=True)
class InputTokenEstimate:
    fingerprint: str
    local_tokens: int
    tokens: int

    def anchor(self, usage: TokenUsage | None) -> InputTokenAnchor | None:
        if usage is None or usage.input_tokens is None:
            return None
        return InputTokenAnchor(self.fingerprint, self.local_tokens, usage.input_tokens)


@dataclass(frozen=True)
class CacheUsage:
    """Session totals for calls reporting both input and cache-read tokens."""

    input_tokens: int
    cache_read_tokens: int


def cache_usage_to_dict(usage: CacheUsage | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {"input_tokens": usage.input_tokens, "cache_read_tokens": usage.cache_read_tokens}


def usage_to_dict(usage: TokenUsage | None) -> dict[str, int | None] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }
