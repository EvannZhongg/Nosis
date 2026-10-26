"""Explicit live calibration; never collected by pytest. No tools are executed."""

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

from agent_core import AgentConfig, LLMRequest, Message, Session, ToolConfig, ToolDefinition, Workspace
from agent_core.context_manager import ContextManager
from agent_core.errors import ProviderError
from agent_core.providers.base import _request_messages, _request_tools
from agent_core.tools import ReadFileTool, ListDirectoryTool, SearchFilesTool, ToolExecutionContext
from agent_runtime.config import load_config
from agent_runtime.plane_manager import ExecutionPlaneManager
from agent_runtime.settings import EnvironmentReloader


def cases():
    simple = ToolDefinition("probe", "Return the requested value.", {
        "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"],
    })
    nested = ToolDefinition("probe", "Return a test result.", {"type": "object", "properties": {
        "options": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["read", "search"]}}, "additionalProperties": False},
    }, "additionalProperties": False})
    context = ToolExecutionContext(workspace=Workspace(Path.cwd()), session=Session())
    builtins = tuple(tool.definition(context) for tool in (ReadFileTool(), ListDirectoryTool(), SearchFilesTool()))
    for name, text, tools in (
        ("no_tools_en", "Reply OK.", ()),
        ("no_tools_zh", "请只回答：好。", ()),
        ("simple_tool", "Reply OK without calling tools.", (simple,)),
        ("nested_tool", "Reply OK without calling tools.", (nested,)),
        ("builtin_tools", "Reply OK without calling tools.", builtins),
        ("long_mixed", ("中文上下文分析。 Code: def f(x): return x + 1.\n" * 300)
         + "Reply OK without calling tools.", builtins),
        ("tool_round", "Call probe with value ping. Do not answer before calling it.", (simple,)),
    ):
        yield name, LLMRequest("Follow the request concisely.", (Message("user", text),), tools)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".nosis")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--compare-litellm", action="store_true")
    parser.add_argument("--thinking", action="store_true")
    args = parser.parse_args()
    EnvironmentReloader(args.config_dir / ".env").reload()
    config = load_config(args.config_dir / "provider_config.json", args.provider)
    policy = AgentConfig(5, 100, ToolConfig(enabled=()), ())
    provider = ExecutionPlaneManager.__new__(ExecutionPlaneManager)._provider_for(
        config, Workspace(Path.cwd()), policy,
    )
    # Disable reasoning to bound cost; the tool-round continuation separately
    # exercises retained reasoning supplied in the actual response, if present.
    provider._reasoning_effort = "low" if args.thinking else "none"
    provider._max_retries = 0
    provider._request_timeout_seconds = 45
    report = {"provider": args.provider, "model": config.model,
              "time_utc": datetime.now(timezone.utc).isoformat(),
              "versions": {name: version(name) for name in ("openai", "tiktoken", "litellm")},
              "cases": []}

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def call(name, request, session):
        context = ContextManager(provider, session, request.system_prompt, "Summarize", policy)
        estimate = context.measure_input_tokens(request)
        row = {"case": name, "local": estimate.local_tokens, "anchored": estimate.tokens,
               "thinking": args.thinking}
        if args.compare_litellm:
            from litellm import token_counter
            row["litellm"] = token_counter(model=config.model,
                messages=_request_messages(request, provider, encode_media=False),
                tools=_request_tools(request) or None)
        report["cases"].append(row)
        started = perf_counter()
        try:
            response = provider.stream(replace(request, max_generation_tokens=2048 if args.thinking else 512), lambda _: None)
            usage = response.usage
            row["usage"] = None if usage is None else {
                "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
            }
            if usage is not None and usage.input_tokens is not None:
                for field in ("local", "anchored", "litellm"):
                    if field in row:
                        row[field + "_error"] = row[field] - usage.input_tokens
            row["tool_calls"] = len(response.tool_calls)
            row["reasoning_characters"] = len(response.reasoning or "")
            session.model_completed(len(report["cases"]), usage=usage,
                has_tool_calls=bool(response.tool_calls), input_token_anchor=estimate.anchor(usage))
            return response
        except ProviderError as error:
            # Error text can contain endpoint details; persist only the type.
            row["error"] = type(error).__name__
            return None
        finally:
            row["seconds"] = round(perf_counter() - started, 3)
            save()
            print(json.dumps(row), flush=True)

    for name, request in cases():
        if args.case and name not in args.case:
            continue
        session = Session()
        response = call(name, request, session)
        if response is None:
            continue
        if response.tool_calls:
            messages = (*request.messages, Message("assistant", response.content,
                reasoning=response.reasoning, tool_calls=response.tool_calls, provider_data=response.provider_data),
                *(Message("tool", '{"value":"ping"}', tool_call_id=tool.id) for tool in response.tool_calls),
                Message("user", "Reply OK. Do not call any more tools."))
        else:
            messages = (*request.messages, Message("assistant", response.content,
                reasoning=response.reasoning, provider_data=response.provider_data), Message("user", "Reply OK again."))
        followup = replace(request, messages=messages)
        response = call(name + "/followup", followup, session)
        if response is not None and name == "builtin_tools":
            # A changed schema must not reuse the prior usage offset.
            call(name + "/changed_tools", replace(followup, tools=()), session)
        if response is not None and name == "long_mixed":
            session.set_archived_summary("Summary: test context.", 0)
            call(name + "/compressed", replace(request, messages=(Message("user", "Summary: test context. Reply OK."),)), session)


if __name__ == "__main__":
    main()
