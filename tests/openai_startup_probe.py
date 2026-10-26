"""Isolated native Runtime request probe; model HTTP is handled in memory."""

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

started = time.perf_counter()
if "--block-litellm" in sys.argv:
    class BlockLiteLLM:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "litellm":
                raise AssertionError("native OpenAI startup imported LiteLLM")
    sys.meta_path.insert(0, BlockLiteLLM())
from tests.bridge_fixtures import open_session_message
from agent_runtime import RuntimeHost
from agent_core import LLMRequest, Message, ToolDefinition
import httpx
from openai import OpenAI

sent = []
def handle(request):
    sent.append(time.perf_counter())
    if "--deepseek" in sys.argv:
        chunk = {"id": "chat_test", "object": "chat.completion.chunk", "created": 1,
                 "model": "deepseek-flash", "choices": [{"index": 0,
                 "delta": {"content": "OK"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101,
                           "prompt_tokens_details": {"cached_tokens": 0}}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
    event = {"type": "response.completed", "sequence_number": 1, "response": {
        "id": "resp_test", "object": "response", "created_at": 1,
        "model": "gpt-6-astra", "status": "completed", "output": [{
            "id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "OK", "annotations": []}],
        }], "usage": {"input_tokens": 100, "output_tokens": 1, "total_tokens": 101,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0}},
    }}
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                         content=("data: " + json.dumps(event) + "\n\n").encode())

def client(**kwargs):
    return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    open_session_message(root, provider_config={
        "main_agent": {"provider": "native", "vision_provider": "native"},
        "fast_model": {"provider": "native"}, "subagent": {"provider": "native"},
        "providers": {"native": ({"model": "deepseek/deepseek-flash", "key": "test-key"}
                                 if "--deepseek" in sys.argv else
                                 {"model": "gpt-6-astra", "key": "test-key", "api": "responses"})},
    }, agent_config={"max_same_tool_calls": 5, "workspace_instruction_files": [],
                     "scratch_workspace_root": str(root / "scratch"),
                     "output_reserve_tokens": 100, "memory": {"enabled": False},
                     "main_agent": {"tools": {"computer_screenshot": True, "computer_action": True,
                                              "generate_image": False, "subagent": False}}})
    host = RuntimeHost(root, start_scheduler=False)
    host.open_session(root)
    turn_start = time.perf_counter()
    try:
        with patch("agent_core.providers.openai_provider.OpenAI", side_effect=client):
            result = host.run_turn("benchmark", "Reply OK without using tools.")
        assert result.status == "completed", result
        provider = host.planes.current.provider
        tools = (ToolDefinition("probe", "Read a file.", {"type": "object", "properties": {"path": {"type": "string"}}}),)
        samples = [provider.count_input_tokens(LLMRequest("Instructions", (Message("user", text),), tools))
                   for text in ("hello world", "分析这个工作区并解释结果。", "def main():\n    return {'ok': True}")]
        print(json.dumps({"process_to_request_seconds": round(sent[0]-started, 3),
                          "turn_to_request_seconds": round(sent[0]-turn_start, 3),
                          "litellm_loaded": "litellm" in sys.modules,
                          "pyautogui_loaded": "pyautogui" in sys.modules,
                          "sample_estimates": samples}))
    finally:
        host.close()
