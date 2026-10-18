"""Model capability fixtures for provider routing and runtime assembly."""

from agent_core import ProviderCapabilities


VISION_MODEL = "vision/model"
TEXT_MODEL = "text/model"


def capabilities_stub(vision_model: str):
    """Build a capability replacement with one image-capable model."""
    def capabilities(cls, model: str, base_url: str | None = None):
        modalities = {"text"}
        if model == vision_model:
            modalities.add("image")
        return ProviderCapabilities(frozenset(modalities))

    return classmethod(capabilities)
