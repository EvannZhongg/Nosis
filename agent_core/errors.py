"""Structured runtime errors persisted by Sessions and exposed by Bridge."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuntimeErrorInfo:
    type: str
    message: str
    details: dict[str, object] = field(default_factory=dict)


class ProviderProtocolError(RuntimeError):
    """A provider stream violated the Tool Call protocol."""

    def __init__(self, message: str, *, details: dict[str, object]) -> None:
        super().__init__(message)
        self.details = details


def runtime_error_info(error: BaseException) -> RuntimeErrorInfo:
    details = error.details if isinstance(error, ProviderProtocolError) else {}
    return RuntimeErrorInfo(
        type=type(error).__name__,
        message=str(error),
        details=dict(details) if isinstance(details, dict) else {},
    )


def runtime_error_from_dict(data: object) -> RuntimeErrorInfo:
    if not isinstance(data, dict):
        raise ValueError("runtime error must be an object")
    error_type = data.get("type")
    message = data.get("message")
    details = data.get("details")
    if not isinstance(error_type, str) or not error_type:
        raise ValueError("runtime error type must be a non-empty string")
    if not isinstance(message, str):
        raise ValueError("runtime error message must be a string")
    if not isinstance(details, dict):
        raise ValueError("runtime error details must be an object")
    return RuntimeErrorInfo(error_type, message, dict(details))
