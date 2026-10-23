"""Session naming from persisted user input and a bounded auxiliary request."""

import json
from typing import Callable

from .auxiliary import AuxiliaryCallTracker
from .content import FilePart, ImagePart, text_content
from .llm import AuxiliaryModelCall, LLMProvider, LLMRequest, with_generation_limit
from .session import Message


TITLE_MAX_CHARS = 80


def initial_session_title(message: Message, session_id: str) -> str:
    text = (text_content(message.content) or "").strip()
    if not text:
        filenames = [
            part.filename for part in message.parts
            if isinstance(part, (ImagePart, FilePart)) and part.filename
        ]
        if filenames:
            text = filenames[0]
            if len(filenames) > 1:
                text += f" 等 {len(filenames)} 个附件"
    return " ".join((text or session_id).split())[:TITLE_MAX_CHARS]


def generate_session_title(
    provider: LLMProvider,
    template: str,
    messages: tuple[Message, ...],
    check_cancelled: Callable[[], None],
    on_auxiliary_call: Callable[[AuxiliaryModelCall], None],
) -> str:
    # Tool output, reasoning and attachment bytes do not help identify a chat.
    conversation = json.dumps([
        {
            "role": message.role,
            "text": (text_content(message.content) or "")[:3000],
            "tools": [call.name for call in message.tool_calls],
            "attachments": [
                part.filename for part in message.parts
                if isinstance(part, (ImagePart, FilePart)) and part.filename
            ],
        }
        for message in messages if message.role in {"user", "assistant"}
        and message.origin == "conversation"
    ], ensure_ascii=False)[:8000]
    prompt = template.replace("{{conversation}}", conversation)
    request = with_generation_limit(
        LLMRequest(system_prompt="", messages=(Message("user", prompt),)), provider, 256,
    )
    with AuxiliaryCallTracker(on_auxiliary_call, task="session_title") as call:
        check_cancelled()
        if provider.count_input_tokens(request) + 256 > provider.max_context_tokens:
            raise ValueError("Session title input exceeds the model context budget")
        call.response = provider.stream_cancellable(
            request, lambda text: None, None, check_cancelled,
        )
        check_cancelled()
        if call.response.tool_calls or not call.response.content:
            raise ValueError("Session title response must contain text only")
        title = " ".join(call.response.content.split())
        if not title or len(title) > TITLE_MAX_CHARS:
            raise ValueError(f"Session title must contain 1–{TITLE_MAX_CHARS} characters")
        return title
