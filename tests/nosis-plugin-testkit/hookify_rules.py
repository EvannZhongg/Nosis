"""Hookify's own project-rule format and matching, outside the Nosis runtime."""

import json
from pathlib import Path
import re


TEST_COMMAND = re.compile(
    r"^\s*(?:python(?:\d+(?:\.\d+)*)?(?:\.exe)?\s+-m\s+(?:pytest|unittest)\b"
    r"|pytest\b|npm\s+(?:run\s+)?test\b|cargo\s+test\b)"
)


def tests_passed(executions: dict) -> bool:
    for tool in executions["tools"]:
        if tool["name"] != "shell" or tool["status"] != "completed":
            continue
        if not TEST_COMMAND.search(tool["arguments"].get("command", "")):
            continue
        result = json.loads(tool["result"])
        output = result["preview"] if "artifact_path" in result else result.get("output", {})
        if output.get("exit_code") == 0 and not output.get("timed_out"):
            return True
    return any(
        job["kind"] == "shell" and job["status"] == "completed"
        and TEST_COMMAND.search(job["details"].get("command", ""))
        and job["result_summary"] is not None
        and job["result_summary"].get("exit_code") == 0
        and not job["result_summary"].get("timed_out")
        for job in executions["jobs"]
    )


def field_value(payload: dict, field: str):
    if field == "tests_passed":
        return tests_passed(payload["executions"])
    value = payload
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def matches(condition: dict, payload: dict) -> bool:
    if set(condition) != {"field", "operator", "value"}:
        raise ValueError("conditions require field, operator and value")
    actual = field_value(payload, condition["field"])
    expected = condition["value"]
    operator = condition["operator"]
    if operator == "equals":
        return actual == expected
    if actual is None:
        return False
    if not isinstance(actual, str) or not isinstance(expected, str):
        raise ValueError("text operators require string values")
    if operator == "regex_match":
        return re.search(expected, actual) is not None
    if operator == "contains":
        return expected in actual
    if operator == "not_contains":
        return expected not in actual
    raise ValueError(f"unknown rule operator: {operator}")


def evaluate(payload: dict, workspace: Path) -> dict:
    event = payload["event"]
    warnings = []
    blocking = []
    for path in sorted((workspace / ".nosis").glob("hookify.*.json")):
        rule = json.loads(path.read_text(encoding="utf-8"))
        if set(rule) - {"name", "event", "enabled", "tools", "action", "conditions", "message"}:
            raise ValueError(f"unknown rule field in {path}")
        if rule.get("enabled", True) is False or rule["event"] != event:
            continue
        if "tools" in rule and payload.get("tool_call", {}).get("name") not in rule["tools"]:
            continue
        conditions = rule["conditions"]
        if not conditions or not all(matches(condition, payload) for condition in conditions):
            continue
        message = f"[{rule['name']}] {rule['message']}"
        action = rule.get("action", "warn")
        if action == "warn":
            warnings.append(message)
        elif action == "block" and event != "PostToolUse":
            blocking.append(message)
        else:
            raise ValueError(f"invalid action for {event}: {action}")
    if blocking:
        message = "\n\n".join(blocking + warnings)
        if event == "Stop":
            return {"decision": "continue", "feedback": message}
        return {"decision": "deny", "reason": message}
    result = {"decision": "pass"}
    if warnings:
        result["feedback"] = "\n\n".join(warnings)
    return result
