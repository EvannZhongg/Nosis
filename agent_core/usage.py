"""Provider-independent token usage and its persisted representation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenUsage:
    # Input totals include cache reads and writes; cache fields are breakdowns.
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    # None means unreported; zero means the provider reported no cache activity.
    cache_read_tokens: int | None
    cache_write_tokens: int | None


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
