import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol, TypeAlias

if TYPE_CHECKING:
    from .context import ToolExecutionContext


JSONValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | list["JSONValue"]
    | dict[str, "JSONValue"]
)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, JSONValue]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, JSONValue]


@dataclass(frozen=True)
class ToolError:
    type: str
    message: str


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    name: str
    output: JSONValue = None
    error: ToolError | None = None

    def to_content(self) -> str:
        if self.error is not None:
            data: dict[str, JSONValue] = {
                "ok": False,
                "error": asdict(self.error),
            }
        else:
            data = {
                "ok": True,
                "output": self.output,
            }
        return json.dumps(data, ensure_ascii=False)


class Tool(ABC):
    """A stateless capability shared by every Agent of a Runtime.

    One instance serves all Agents and may run on several threads at once,
    so a Tool must hold no invocation state: everything a call needs comes
    from ``arguments`` and the :class:`ToolExecutionContext`. Both the
    schema and the execution therefore take the context, which lets one
    shared instance describe itself differently per Runtime.
    """

    #: Catalog key. Instances may override it when the name is discovered
    #: at runtime, as MCP tools do.
    name: ClassVar[str]

    #: Whether a batch of calls to this tool may run on separate threads.
    concurrent: ClassVar[bool] = False

    @abstractmethod
    def definition(self, context: "ToolExecutionContext") -> ToolDefinition:
        raise NotImplementedError

    def available(self, context: "ToolExecutionContext") -> bool:
        """Whether this Runtime supplies the dependencies the tool needs."""
        return True

    @abstractmethod
    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: "ToolExecutionContext",
    ) -> JSONValue:
        raise NotImplementedError


class ToolPolicy(Protocol):
    def authorize(self, call: ToolCall) -> None:
        raise NotImplementedError
