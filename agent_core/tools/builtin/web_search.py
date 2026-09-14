from exa_py import Exa

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext


DEFAULT_NUM_RESULTS = 5
MAX_NUM_RESULTS = 10
# Excerpts are capped so a full result set stays inside the tool result
# limit that the agent can feed back to the model.
MAX_HIGHLIGHT_CHARACTERS = 1000


class WebSearchTool(Tool):
    name = "web_search"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Search the web for current information and return ranked "
                "results, each with its title, URL, publication date and the "
                "most relevant excerpts of the page."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Web search query.",
                    },
                    "num_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_NUM_RESULTS,
                        "default": DEFAULT_NUM_RESULTS,
                        "description": "Maximum number of results to return.",
                    },
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
                },
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
        include_domains = _parse_domains(
            arguments.get("include_domains"),
            "include_domains",
        )
        exclude_domains = _parse_domains(
            arguments.get("exclude_domains"),
            "exclude_domains",
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
        if not set(arguments) <= {
            "query",
            "num_results",
            "include_domains",
            "exclude_domains",
        }:
            raise ValueError(
                "web_search accepts only 'query', 'num_results', "
                "'include_domains', and 'exclude_domains'"
            )

        # Built per call: the tool instance is shared by every Agent of
        # the Runtime and may run on several threads, so it caches nothing.
        # A missing EXA_API_KEY therefore fails this call rather than
        # preventing the agent from starting.
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

        return {
            "query": query,
            "results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "published_date": result.published_date,
                    "highlights": result.highlights,
                }
                for result in response.results
            ],
        }


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
