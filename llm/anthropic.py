"""
Anthropic extractor.

Uses structured outputs (`output_config.format`) rather than a forced tool
call: the API constrains the response to the JSON Schema directly, which is
what tool-call-smuggling used to be a workaround for.

Deliberately no extended thinking. Extraction is perception, not reasoning --
the answer is on the page. Thinking would add latency and tokens for a task
whose difficulty is reading, not deducing.
"""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal

import anthropic

from core.config import get_settings
from llm.base import ExtractionError, ExtractionRefused, ExtractionResult

log = logging.getLogger("llm")

#: USD per million tokens, (input, output, cache_read). Kept here rather than
#: fetched so a cost figure is never silently wrong when the network is down.
PRICING: dict[str, tuple[str, str, str]] = {
    "claude-haiku-4-5": ("1.00", "5.00", "0.10"),
    "claude-sonnet-5": ("2.00", "10.00", "0.20"),
    "claude-opus-5": ("5.00", "25.00", "0.50"),
}

DEFAULT_MODEL = "claude-haiku-4-5"

# Extraction output is small even with line items; this is headroom, not a
# target. Well under the streaming threshold, so a plain request is fine.
MAX_TOKENS = 4096

SYSTEM_PROMPT = """\
You extract structured data from documents that have been read by OCR.

Rules:
- Use only what appears in the text. Never infer, calculate, or invent a value.
- Each field's description tells you what to look for. Follow it exactly.
- If a value is not present in the text, return null for that field. A null is
  correct and useful; a guess is not.
- Copy amounts exactly as printed, including the decimal separator used.
- OCR output is imperfect: characters may be wrong and columns may be
  flattened into reading order. Do not repair text beyond reading it.\
"""


def price(model: str, usage) -> Decimal:
    """Cost of one call, in USD, as an exact Decimal."""
    if model not in PRICING:
        return Decimal("0")
    per_in, per_out, per_cache = (Decimal(p) for p in PRICING[model])
    million = Decimal("1000000")
    cached = Decimal(getattr(usage, "cache_read_input_tokens", 0) or 0)
    return (
        Decimal(usage.input_tokens) * per_in
        + Decimal(usage.output_tokens) * per_out
        + cached * per_cache
    ) / million


class AnthropicExtractor:
    """Structured extraction against the Anthropic API."""

    def __init__(self, model: str = DEFAULT_MODEL, client: anthropic.Anthropic | None = None):
        self.model = model
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        # Built lazily so importing this module never requires a key -- the
        # API and worker must start without one.
        if self._client is None:
            key = get_settings().anthropic_api_key
            if not key:
                raise ExtractionError("ANTHROPIC_API_KEY is not set")
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def extract(self, *, json_schema: dict, text: str, doc_type: str) -> ExtractionResult:
        started = time.perf_counter()
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _user_message(doc_type, text)}],
                output_config={"format": {"type": "json_schema", "schema": json_schema}},
            )
        except anthropic.RateLimitError as exc:
            raise ExtractionError(f"rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            # 4xx is our request being wrong and will not fix itself; 5xx might.
            if exc.status_code < 500:
                raise ExtractionRefused(f"request rejected ({exc.status_code}): {exc}") from exc
            raise ExtractionError(f"server error ({exc.status_code}): {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise ExtractionError(f"connection failed: {exc}") from exc
        except anthropic.AnthropicError as exc:
            # Everything else the SDK can raise. Three families reach here and
            # none of them subclass the clauses above: APIResponseValidationError
            # is an APIError rather than an APIStatusError, and RetryableError
            # and the credential errors descend straight from AnthropicError.
            # RetryableError in particular is raised once the SDK has spent its
            # own retries -- by name, exactly the case the worker exists to
            # handle -- so letting it escape as a bare SDK exception would skip
            # the retryable/permanent split entirely and the worker would
            # neither retry it nor fail the job deliberately.
            #
            # Transient is the safe default: something permanent will exhaust
            # max_attempts and reach the same place, a few wasted calls later.
            raise ExtractionError(f"{type(exc).__name__}: {exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "category", None)
            raise ExtractionRefused(f"model declined to answer (category={detail})")
        if response.stop_reason == "max_tokens":
            # Truncated JSON is unparseable; more attempts at the same limit
            # will truncate identically.
            raise ExtractionRefused(f"output hit max_tokens ({MAX_TOKENS}); schema too large")

        payload = next((b.text for b in response.content if b.type == "text"), None)
        if payload is None:
            raise ExtractionError("response contained no text block")

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            # Should be impossible under structured outputs, so treat it as a
            # transient API fault rather than a permanent one.
            raise ExtractionError(f"structured output was not valid JSON: {exc}") from exc

        return ExtractionResult(
            data=data,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cost_usd=price(self.model, response.usage),
            latency_ms=elapsed_ms,
            stop_reason=response.stop_reason,
            params={"max_tokens": MAX_TOKENS, "thinking": "off", "doc_type": doc_type},
        )


def _user_message(doc_type: str, text: str) -> str:
    """
    Only the volatile part. Stable guidance lives in the system prompt so that
    it stays a constant prefix -- which is what prompt caching would need if
    the prompt ever grows past the minimum cacheable size.
    """
    return f"Document type: {doc_type}\n\nOCR text:\n\n{text}"
