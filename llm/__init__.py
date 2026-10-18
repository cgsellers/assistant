"""The model boundary: extraction behind a protocol."""

from __future__ import annotations

from llm.base import ExtractionError, ExtractionRefused, ExtractionResult, Extractor


def get_extractor(model: str | None = None) -> Extractor:
    """
    Build the configured extractor.

    One provider exists today, so this is a seam rather than a registry. It
    earns its place anyway: the phase 3 eval harness has to run the same
    documents through different tiers, and the worker is the last thing that
    should know which tier it got.

    Construction touches neither the network nor the key -- AnthropicExtractor
    builds its client at first use -- so a worker that only ever sees OCR jobs
    pays nothing for this, and still starts with no key configured.
    """
    from core.config import get_settings
    from llm.anthropic import AnthropicExtractor

    return AnthropicExtractor(model=model or get_settings().extraction_model)


__all__ = [
    "ExtractionError",
    "ExtractionRefused",
    "ExtractionResult",
    "Extractor",
    "get_extractor",
]
