import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, ClassVar, Protocol, TypeAlias

from ..content import ImagePart

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
class ToolError:
    type: str
    message: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    # Invalid arguments retain the provider's exact text for history and repair.
    arguments: dict[str, JSONValue] | str
    argument_error: ToolError | None = None

    @classmethod
    def from_raw_arguments(cls, id: str, name: str, raw: str) -> "ToolCall":
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError as error:
            message = str(error)
        else:
            if isinstance(arguments, dict):
                return cls(id, name, arguments)
            message = "arguments must be a JSON object"
        return cls(
            id, name, raw,
            ToolError(
                type="invalid_tool_arguments",
                message=(
                    f"Invalid arguments for tool '{name}': {message}. "
                    "Send a valid JSON object and call the tool again."
                ),
            ),
        )

    @property
    def arguments_json(self) -> str:
        if isinstance(self.arguments, str):
            return self.arguments
        return json.dumps(self.arguments, ensure_ascii=False)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id, "name": self.name, "arguments": self.arguments,
        }
        if self.argument_error is not None:
            data["argument_error"] = asdict(self.argument_error)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ToolCall":
        error = data.get("argument_error")
        return cls(
            data["id"], data["name"], data["arguments"],
            ToolError(**error) if error is not None else None,
        )


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, JSONValue]


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    name: str
    output: JSONValue = None
    error: ToolError | None = None
    #: Images this call wants placed in the model's context.  They are
    #: delivered as a separate message after the tool results, because a
    #: tool-role message cannot carry image content on the
    #: OpenAI-compatible chat API that providers are reached through.
    attachments: tuple[ImagePart, ...] = ()
    # Internal hooks used by tools that spool large results.  They are not
    # part of the serialized tool result; the Runtime consumes them when it
    # normalizes the result into a Session artifact.
    artifact_writer: Callable[[Path], int] | None = field(
        default=None, compare=False, repr=False
    )
    artifact_cleanup: Callable[[], None] | None = field(
        default=None, compare=False, repr=False
    )
    # Called with json_budget for the returned JSONValue alone. Must satisfy
    # len(json.dumps(value, ensure_ascii=False)) <= json_budget, including
    # JSON syntax, string quotes, and escapes; not the raw string length.
    artifact_preview: Callable[[int], JSONValue] | None = field(
        default=None, compare=False, repr=False
    )

    def __del__(self) -> None:
        # Last-resort cleanup when a tool result is abandoned before the
        # normalizer (for example, cancellation or a failed batch commit).
        cleanup = self.artifact_cleanup
        if cleanup is not None:
            try:
                cleanup()
            except BaseException:
                pass

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


@dataclass(frozen=True)
class ToolOutput:
    """A tool's result when it carries attachments or a spooled artifact.

    A tool returns this instead of a bare :data:`JSONValue` when the
    model needs to see pixels or when a complete result is retained on
    disk: ``output`` is serialized into the tool result as usual, while
    ``attachments`` and artifact hooks are consumed by the Runtime.
    """

    output: JSONValue = None
    attachments: tuple[ImagePart, ...] = ()
    artifact_writer: Callable[[Path], int] | None = field(
        default=None, compare=False, repr=False
    )
    artifact_cleanup: Callable[[], None] | None = field(
        default=None, compare=False, repr=False
    )
    # Called with json_budget for the returned JSONValue alone. Must satisfy
    # len(json.dumps(value, ensure_ascii=False)) <= json_budget, including
    # JSON syntax, string quotes, and escapes; not the raw string length.
    artifact_preview: Callable[[int], JSONValue] | None = field(
        default=None, compare=False, repr=False
    )


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
    ) -> "JSONValue | ToolOutput":
        raise NotImplementedError


class ToolPolicy(Protocol):
    def authorize(
        self,
        call: ToolCall,
        context: "ToolExecutionContext",
    ) -> bool | None:
        """Authorize a call and report whether the user was consulted."""
        raise NotImplementedError
