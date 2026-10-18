from typing import cast
from urllib.parse import urlsplit

import httpx

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext


DEFAULT_NUM_RESULTS = 5
MAX_NUM_RESULTS = 10
# Excerpts are capped so a full result set stays inside the tool result
# limit that the agent can feed back to the model.
MAX_HIGHLIGHT_CHARACTERS = 1000
DEEPSEEK_MAX_QUERY_CHARACTERS = 128
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
EXA_SEARCH_URL = "https://api.exa.ai/search"


class WebSearchTool(Tool):
    name = "web_search"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        provider = context.tool_settings.web_search.provider
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
        elif provider == "deepseek":
            query_schema = cast(dict[str, JSONValue], properties["query"])
            query_schema.update({
                "minLength": 1,
                "maxLength": DEEPSEEK_MAX_QUERY_CHARACTERS,
                "description": (
                    "Web search query, 1-128 Unicode characters, not all "
                    "whitespace. Supports site:domain, -site:domain, quoted "
                    "phrases, -keyword, lang:zh,en, after:YYYY-MM-DD and "
                    "before:YYYY-MM-DD. Combine sites with OR, for example "
                    "'python site:docs.python.org OR site:python.org'."
                ),
            })

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

        config = context.tool_settings.web_search
        provider = config.provider
        include_domains = exclude_domains = None
        if provider == "exa":
            include_domains = _parse_domains(
                arguments.get("include_domains"), "include_domains",
            )
            exclude_domains = _parse_domains(
                arguments.get("exclude_domains"), "exclude_domains",
            )
        elif provider == "deepseek":
            if not query.strip() or len(query) > DEEPSEEK_MAX_QUERY_CHARACTERS:
                raise ValueError(
                    "web_search requires 'query' to be 1-128 Unicode characters "
                    "and not all whitespace for DeepSeek"
                )

        fallback_reason = None
        if provider != "duckduckgo" and not config.api_key:
            fallback_reason = "missing_api_key"
        else:
            try:
                if provider == "exa":
                    results = _search_exa(
                        query, num_results, include_domains, exclude_domains,
                        cast(str, config.api_key),
                    )
                elif provider == "deepseek":
                    results = _search_deepseek(
                        query, num_results, cast(str, config.api_key),
                    )
                else:
                    results = _search_duckduckgo(query, num_results)
            except httpx.HTTPStatusError as error:
                if (
                    provider == "duckduckgo"
                    or error.response.status_code != 401
                ):
                    raise
                fallback_reason = "authentication_failed"

        if fallback_reason is not None:
            results = _search_duckduckgo(
                query, num_results,
                include_domains=include_domains, exclude_domains=exclude_domains,
            )
            return {
                "query": query,
                "results": results,
                "provider": "duckduckgo",
                "fallback": {"from": provider, "reason": fallback_reason},
            }

        return {"query": query, "results": results}


def _search_duckduckgo(
    query: str,
    num_results: int,
    *,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> list[JSONValue]:
    from ddgs import DDGS

    response = DDGS(timeout=10).text(
        query, max_results=num_results, backend="duckduckgo",
    )
    included = tuple(domain.lower().rstrip(".") for domain in include_domains or ())
    excluded = tuple(domain.lower().rstrip(".") for domain in exclude_domains or ())
    results: list[JSONValue] = []
    for item in response:
        # Keep the query's own operators intact. Structured filters restrict
        # returned URLs independently, so they cannot broaden an existing site:.
        if included or excluded:
            host = (urlsplit(item.get("href", "")).hostname or "").rstrip(".")
            if included and not any(
                host == domain or host.endswith("." + domain) for domain in included
            ):
                continue
            if any(host == domain or host.endswith("." + domain) for domain in excluded):
                continue
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
    api_key: str,
) -> list[JSONValue]:
    payload: dict[str, JSONValue] = {
        "query": query,
        "type": "auto",
        "numResults": num_results,
        "contents": {"highlights": {"maxCharacters": MAX_HIGHLIGHT_CHARACTERS}},
    }
    if include_domains is not None:
        payload["includeDomains"] = include_domains
    if exclude_domains is not None:
        payload["excludeDomains"] = exclude_domains
    # Use HTTP status codes for authentication fallback; the SDK flattens
    # upstream failures into ValueError strings.
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            EXA_SEARCH_URL,
            headers={"x-api-key": api_key},
            json=payload,
        )
        response.raise_for_status()
        items = response.json()["results"]

    return [
        {
            "title": item.get("title"),
            "url": item.get("url"),
            "published_date": item.get("publishedDate"),
            "highlights": item.get("highlights"),
        }
        for item in items[:num_results]
    ]


def _search_deepseek(query: str, num_results: int, api_key: str) -> list[JSONValue]:
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            DEEPSEEK_SEARCH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "count": num_results, "need_content": False},
        )
        response.raise_for_status()
        items = response.json()["results"]

    results: list[JSONValue] = []
    for item in items[:num_results]:
        summary = item["summary"][:MAX_HIGHLIGHT_CHARACTERS]
        results.append({
            "title": item["title"],
            "url": item["url"],
            "published_date": item["date"],
            "highlights": [summary] if summary else None,
        })
    return results


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
