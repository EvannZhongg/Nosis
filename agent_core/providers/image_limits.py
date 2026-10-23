"""Image dimension limits at known service endpoints, before wire encoding."""

from urllib.parse import urlsplit


def image_edge_limit(model: str, base_url: str | None, image_count: int) -> int | None:
    """Return the upload edge limit; custom endpoints keep their own behavior.

    These are rejection limits, not the smaller resolutions used internally
    for inference. Count images across the entire outgoing conversation.
    """
    route, separator, name = model.partition("/")
    if not separator:
        name = model
        route = ""
    if base_url is not None:
        host = urlsplit(base_url).hostname
        service = {
            "api.deepseek.com": "deepseek",
            "api.anthropic.com": "anthropic",
        }.get(host)
    else:
        service = route

    # https://api-docs.deepseek.com/zh-cn/guides/vision/#限制
    if service == "deepseek" and name in {
        "deepseek-flash", "deepseek-v4-flash-vision-exp",
    }:
        return 4096 if image_count >= 15 else 8192
    # https://platform.claude.com/docs/en/build-with-claude/vision#request-limits
    if service == "anthropic" and name.startswith("claude-"):
        return 2000 if image_count > 20 else 8000
    return None
