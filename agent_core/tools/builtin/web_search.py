from typing import cast

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from . import web_fetch


DEFAULT_NUM_RESULTS = 5
MAX_NUM_RESULTS = 10
# Excerpts are capped so a full result set stays inside the tool result
# limit that the agent can feed back to the model.
MAX_HIGHLIGHT_CHARACTERS = 1000


class WebSearchTool(Tool):
    name = "web_search"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        provider = web_fetch.WEB_SEARCH_PROVIDER
        query_description = (
            "Web search query. Use site:domain to search a specific "
            "site, for example 'python site:docs.python.org'."
            if provider == "duckduckgo"
            else "Web search query."
        )
        properties: dict[str, JSONValue] = {
            "query": {
                "type": "string",
                "description": query_description,
            },
            "num_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_NUM_RESULTS,
                "default": DEFAULT_NUM_RESULTS,
                "description": "Maximum number of results to return.",
            },
        }
        if provider == "exa":
            properties.update({
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Return results only from these domains, for "
                        "example ['docs.python.org']."
                    ),
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Never return results from these domains.",
                },
            })
        elif provider != "duckduckgo":
            raise ValueError(f"Unsupported web search provider: {provider}")

        return ToolDefinition(
            name=self.name,
            description=(
                "Search the web for current information and return ranked "
                "results with titles, URLs and relevant excerpts. "
                "Publication dates are included when available."
            ),
            parameters={
                "type": "object",
                "properties": properties,
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        query = arguments.get("query")
        num_results = arguments.get("num_results", DEFAULT_NUM_RESULTS)
        properties = cast(
            dict[str, JSONValue], self.definition(context).parameters["properties"],
        )

        if not isinstance(query, str) or not query:
            raise ValueError("web_search requires a non-empty string 'query'")
        if (
            isinstance(num_results, bool)
            or not isinstance(num_results, int)
            or num_results < 1
            or num_results > MAX_NUM_RESULTS
        ):
            raise ValueError(
                "web_search requires 'num_results' to be an integer between "
                f"1 and {MAX_NUM_RESULTS}"
            )
        if not arguments.keys() <= properties.keys():
            allowed = ", ".join(repr(name) for name in properties)
            raise ValueError(f"web_search accepts only {allowed}")

        provider = web_fetch.WEB_SEARCH_PROVIDER
        if provider == "duckduckgo":
            results = _search_duckduckgo(query, num_results)
        elif provider == "exa":
            include_domains = _parse_domains(
                arguments.get("include_domains"), "include_domains",
            )
            exclude_domains = _parse_domains(
                arguments.get("exclude_domains"), "exclude_domains",
            )
            results = _search_exa(
                query, num_results, include_domains, exclude_domains,
            )
        else:
            raise ValueError(f"Unsupported web search provider: {provider}")

        return {"query": query, "results": results}


def _search_duckduckgo(
    query: str,
    num_results: int,
) -> list[JSONValue]:
    from ddgs import DDGS

    response = DDGS(timeout=10).text(
        query, max_results=num_results, backend="duckduckgo",
    )
    results: list[JSONValue] = []
    for item in response:
        snippet = item.get("body", "")[:MAX_HIGHLIGHT_CHARACTERS]
        results.append({
            "title": item.get("title", ""),
            "url": item.get("href", ""),
            "published_date": None,
            "highlights": [snippet] if snippet else None,
        })
    return results[:num_results]


def _search_exa(
    query: str,
    num_results: int,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
) -> list[JSONValue]:
    # Clients are built per call because the tool is shared by Agents.
    # A missing EXA_API_KEY fails this call rather than agent startup.
    from exa_py import Exa

    response = Exa().search(
        query,
        type="auto",
        num_results=num_results,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        contents={
            "highlights": {"max_characters": MAX_HIGHLIGHT_CHARACTERS}
        },
    )

    return [
        {
            "title": result.title,
            "url": result.url,
            "published_date": result.published_date,
            "highlights": result.highlights,
        }
        for result in response.results
    ]


def _parse_domains(value: JSONValue, argument: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError(
            f"web_search requires '{argument}' to be a non-empty array of "
            "domains"
        )

    domains = []
    for domain in value:
        if not isinstance(domain, str) or not domain:
            raise ValueError(
                f"web_search requires '{argument}' to contain non-empty "
                "strings"
            )
        domains.append(domain)
    return domains
