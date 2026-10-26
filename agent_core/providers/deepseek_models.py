"""DeepSeek's published model snapshot (2026-10-26).

https://api-docs.deepseek.com/quick_start/pricing/
https://api-docs.deepseek.com/api/create-chat-completion/
"""


def is_deepseek(model: str) -> bool:
    return model.startswith("deepseek/") or model in {"deepseek-flash", "deepseek-v4-pro"}


def model_info(model: str) -> dict[str, object]:
    name = model.removeprefix("deepseek/")
    if name not in {"deepseek-flash", "deepseek-v4-pro"}:
        return {}
    return {"max_input_tokens": 1_000_000, "max_output_tokens": 393_216,
            "supports_vision": name == "deepseek-flash"}
