"""Small helpers shared by provider adapters."""


def get_field(value: object, name: str) -> object:
    """Read a field from either a mapping or an object-style response."""
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
