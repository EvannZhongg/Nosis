"""Local OpenAI model limits; unknown names require an explicit context limit.

Limits are a release snapshot, not discovered over the network:
https://developers.openai.com/api/docs/models
The first value is the input ceiling (not input plus maximum output).
GPT-6 tokenization is not mapped by tiktoken 0.14; cl100k remains the
existing local estimate until an official encoding is published.
"""

# model: (maximum input tokens, maximum output tokens, vision)
_MODELS = {
    'computer-use-preview': (8192, 1024, True),
    'computer-use-preview-2025-03-11': (8192, 1024, True),
    'gpt-3.5-turbo': (16385, 4096, False),
    'gpt-3.5-turbo-0125': (16385, 4096, False),
    'gpt-3.5-turbo-1106': (16385, 4096, False),
    'gpt-3.5-turbo-16k': (16385, 4096, False),
    'gpt-4': (8192, 4096, False),
    'gpt-4-0613': (8192, 4096, False),
    'gpt-4-1106-preview': (128000, 4096, False),
    'gpt-4-turbo': (128000, 4096, True),
    'gpt-4-turbo-2024-04-09': (128000, 4096, True),
    'gpt-4.1': (1047576, 32768, True),
    'gpt-4.1-2025-04-14': (1047576, 32768, True),
    'gpt-4.1-mini': (1047576, 32768, True),
    'gpt-4.1-mini-2025-04-14': (1047576, 32768, True),
    'gpt-4.1-nano': (1047576, 32768, True),
    'gpt-4.1-nano-2025-04-14': (1047576, 32768, True),
    'gpt-4o': (128000, 16384, True),
    'gpt-4o-2024-05-13': (128000, 4096, True),
    'gpt-4o-2024-08-06': (128000, 16384, True),
    'gpt-4o-2024-11-20': (128000, 16384, True),
    'gpt-4o-mini': (128000, 16384, True),
    'gpt-4o-mini-2024-07-18': (128000, 16384, True),
    'gpt-5': (272000, 128000, True),
    'gpt-5.1': (272000, 128000, True),
    'gpt-5.1-chat-latest': (128000, 16384, True),
    'gpt-5.1-2025-11-13': (272000, 128000, True),
    'gpt-5.2': (272000, 128000, True),
    'gpt-5.2-chat-latest': (128000, 16384, True),
    'gpt-5.2-2025-12-11': (272000, 128000, True),
    'gpt-6-astra': (922000, 128000, True),
    'gpt-6-sol': (922000, 128000, True),
    'gpt-6-luna': (922000, 128000, True),
    'gpt-5.6': (922000, 128000, True),
    'gpt-5.6-sol': (922000, 128000, True),
    'gpt-5.6-terra': (922000, 128000, True),
    'gpt-5.6-luna': (922000, 128000, True),
    'gpt-5.6-cyber': (400000, 128000, True),
    'daybreak-red-latest': (400000, 128000, True),
    'daybreak-blue-latest': (922000, 128000, True),
    'chat-latest': (400000, 128000, True),
    'gpt-5.5': (1050000, 128000, True),
    'gpt-5.5-2026-04-23': (1050000, 128000, True),
    'gpt-5.4': (1050000, 128000, True),
    'gpt-5.4-2026-03-05': (1050000, 128000, True),
    'gpt-5.4-mini': (272000, 128000, True),
    'gpt-5.4-mini-2026-03-17': (272000, 128000, True),
    'gpt-5.4-nano': (272000, 128000, True),
    'gpt-5.4-nano-2026-03-17': (272000, 128000, True),
    'gpt-5-2025-08-07': (272000, 128000, True),
    'gpt-5-chat': (128000, 16384, True),
    'gpt-5-chat-latest': (128000, 16384, True),
    'gpt-5.3-chat-latest': (128000, 16384, True),
    'gpt-5-mini': (272000, 128000, True),
    'gpt-5-mini-2025-08-07': (272000, 128000, True),
    'gpt-5-nano': (272000, 128000, True),
    'gpt-5-nano-2025-08-07': (272000, 128000, True),
    'o1': (200000, 100000, True),
    'o1-2024-12-17': (200000, 100000, True),
    'o3': (200000, 100000, True),
    'o3-2025-04-16': (200000, 100000, True),
    'o3-mini': (200000, 100000, False),
    'o3-mini-2025-01-31': (200000, 100000, False),
    'o4-mini': (200000, 100000, True),
    'o4-mini-2025-04-16': (200000, 100000, True),
    'gpt-6.1-sol': (922000, 128000, True),
}


def model_name(model: str) -> str | None:
    route, separator, name = model.partition("/")
    return name if route == "openai" and separator else model if not separator else None


def model_info(model: str) -> dict[str, object]:
    values = _MODELS.get(model)
    if values is None:
        return {}
    context, output, vision = values
    return {"max_input_tokens": context, "max_output_tokens": output, "supports_vision": vision}

