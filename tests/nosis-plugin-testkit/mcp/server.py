"""A normal MCP server; no Nosis imports or access to Runtime objects."""

import ast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


server = MCPServer("repo-review-kit")


@server.tool(description="Analyze Python source for syntax errors, eval calls and bare except handlers")
def analyze_code(source: str) -> dict[str, object]:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return {"findings": [{"line": error.lineno, "message": error.msg}], "count": 1}
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "eval":
            findings.append({"line": node.lineno, "message": "Review eval input: it executes Python expressions."})
        elif isinstance(node, ast.ExceptHandler) and node.type is None:
            findings.append({"line": node.lineno, "message": "Bare except also catches process interruption."})
    findings.sort(key=lambda finding: finding["line"])
    return {"findings": findings, "count": len(findings)}


@server.tool(description="Return a deliberate tool error to verify isolation")
def fail_review() -> str:
    raise ToolError("Deliberate review failure from the testkit")


@server.tool(description="A probe excluded by the plugin's MCP allowlist")
def internal_probe() -> str:
    return "This tool should not be exposed by Nosis."


if __name__ == "__main__":
    server.run("stdio")
