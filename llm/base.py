"""
The model boundary.

Extraction is the only model call in phase 2. Keeping it behind a protocol
means the eval harness can swap providers and tiers over the same documents,
and the worker never learns which model ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable


class ExtractionError(Exception):
    """
    The call failed in a way worth recording on the document.

    `spent` is set when the failure happened *after* a response came back.
    Those calls were billed -- a refusal or a max_tokens truncation is an
    HTTP 200 with a full usage block -- and if the error did not carry the
    usage, the cost would vanish from every report. It is None when nothing
    reached the model (a dropped connection, a 4xx on the request itself),
    because then nothing was charged.
    """

    def __init__(self, message: str, *, spent: ExtractionResult | None = None):
        super().__init__(message)
        self.spent = spent


class ExtractionRefused(ExtractionError):
    """The model declined. Permanent for these bytes -- do not retry."""


@dataclass(frozen=True)
class ExtractionResult:
    """
    What one extraction attempt produced, plus what it cost.

    The cost fields are not telemetry -- they are the numbers the project is
    meant to report, so they are part of the result rather than logged and
    forgotten.
    """

    data: dict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    stop_reason: str | None = None
    #: Anything needed to reproduce the call in an eval run.
    params: dict = field(default_factory=dict)


@runtime_checkable
class Extractor(Protocol):
    """Turns document text into JSON matching a schema."""

    model: str

    def extract(self, *, json_schema: dict, text: str, doc_type: str) -> ExtractionResult:
        """
        Extract `text` into the shape `json_schema` describes.

        The schema's field descriptions are the instructions -- they are what
        tells the model where to look -- so callers should build the schema
        with any issuer profile already applied.

        Raises ExtractionRefused for a permanent refusal, ExtractionError for
        anything retryable.
        """
        ...
