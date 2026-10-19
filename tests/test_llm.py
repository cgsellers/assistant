"""The model boundary: request shape, cost arithmetic, and the retryable/permanent split."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from llm.anthropic import (
    DEFAULT_MODEL,
    MAX_TOKENS,
    PRICING,
    SYSTEM_PROMPT,
    AnthropicExtractor,
    price,
    pricing_key,
)
from llm.base import ExtractionError, ExtractionRefused, ExtractionResult, Extractor

SCHEMA = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string", "description": "The name printed at the top"},
        "total": {"type": ["string", "null"], "description": "The grand total as printed"},
    },
    "required": ["merchant", "total"],
    "additionalProperties": False,
}
OCR_TEXT = "CAFE LUNA\nTOTAL 9.35"
PAYLOAD = {"merchant": "CAFE LUNA", "total": "9.35"}

# anthropic 1.x is built on the httpx 2.x fork, which installs under the name
# `httpx2` and is a different package from the `httpx` the OCR client uses.
# Its status errors read `status_code` and `headers` off a real response, so
# one is constructed rather than stubbed.
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

_ABSENT = object()


def make_usage(input_tokens: int = 0, output_tokens: int = 0, cache_read=_ABSENT):
    """
    `price` reads the cache field through getattr, so the attribute-missing
    shape has to be expressible here; a SimpleNamespace can omit an attribute,
    the SDK's Usage model cannot.
    """
    fields = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if cache_read is not _ABSENT:
        fields["cache_read_input_tokens"] = cache_read
    return SimpleNamespace(**fields)


def make_response(
    *,
    content=_ABSENT,
    model: str = "claude-haiku-4-5",
    usage=None,
    stop_reason: str | None = "end_turn",
    stop_details=None,
):
    """A Message shaped like the SDK's, down to the attributes `extract` reads."""
    if content is _ABSENT:
        content = [SimpleNamespace(type="text", text=json.dumps(PAYLOAD))]
    return SimpleNamespace(
        content=content,
        model=model,
        usage=usage if usage is not None else make_usage(1200, 300, 0),
        stop_reason=stop_reason,
        stop_details=stop_details,
    )


class FakeMessages:
    """Records the request and returns a canned response, or raises."""

    def __init__(self, response=None, raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.response


class FakeAnthropic:
    """
    Stands in for anthropic.Anthropic. Injected through the constructor, which
    is the only reason that argument exists: no test may reach the network, and
    the suite must run with no ANTHROPIC_API_KEY set.
    """

    def __init__(self, response=None, raises: Exception | None = None):
        self.messages = FakeMessages(response, raises)


def status_error(cls: type[anthropic.APIStatusError], status_code: int):
    return cls("boom", response=httpx2.Response(status_code, request=REQUEST), body=None)


def extract_with(client: FakeAnthropic, *, model: str = DEFAULT_MODEL, doc_type: str = "receipt"):
    return AnthropicExtractor(model=model, client=client).extract(
        json_schema=SCHEMA, text=OCR_TEXT, doc_type=doc_type
    )


def forbid_settings(*args, **kwargs):
    raise AssertionError("configuration was read when it should not have been")


class TestProtocolConformance:
    def test_satisfies_the_extractor_protocol(self):
        """runtime_checkable Protocol -- swappability checked, not assumed."""
        assert isinstance(AnthropicExtractor(), Extractor)

    def test_defaults_to_the_cheap_model(self):
        assert AnthropicExtractor().model == DEFAULT_MODEL
        assert DEFAULT_MODEL in PRICING


class TestLazyClient:
    """The API and worker import this module at startup, before any key exists."""

    def test_constructing_reads_no_configuration(self, monkeypatch):
        monkeypatch.setattr("llm.anthropic.get_settings", forbid_settings)
        assert AnthropicExtractor().model == DEFAULT_MODEL

    def test_an_injected_client_bypasses_configuration_entirely(self, monkeypatch):
        monkeypatch.setattr("llm.anthropic.get_settings", forbid_settings)
        assert extract_with(FakeAnthropic(make_response())).data == PAYLOAD

    def test_touching_the_client_without_a_key_raises(self, settings, monkeypatch):
        from core.config import get_settings

        # Empty rather than deleted: a .env file on the developer's machine
        # would otherwise supply the key the test is trying to remove.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        get_settings.cache_clear()
        with pytest.raises(ExtractionError, match="ANTHROPIC_API_KEY"):
            _ = AnthropicExtractor().client

    def test_extracting_without_a_key_raises_rather_than_calling(self, settings, monkeypatch):
        from core.config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        get_settings.cache_clear()
        with pytest.raises(ExtractionError, match="ANTHROPIC_API_KEY"):
            AnthropicExtractor().extract(json_schema=SCHEMA, text=OCR_TEXT, doc_type="receipt")

    def test_an_injected_client_is_used_as_given(self):
        client = FakeAnthropic(make_response())
        assert AnthropicExtractor(client=client).client is client


class TestSuccessfulExtraction:
    @pytest.fixture
    def result(self) -> ExtractionResult:
        response = make_response(
            model="claude-haiku-4-5",
            usage=make_usage(1234, 567, 8900),
            stop_reason="end_turn",
        )
        return extract_with(FakeAnthropic(response))

    def test_returns_the_parsed_payload(self, result):
        assert isinstance(result, ExtractionResult)
        assert result.data == PAYLOAD

    def test_reports_the_model_the_api_answered_with(self, result):
        assert result.model == "claude-haiku-4-5"

    def test_carries_the_token_counts(self, result):
        assert (result.input_tokens, result.output_tokens) == (1234, 567)
        assert result.cache_read_tokens == 8900

    def test_prices_the_call(self, result):
        # 1234 in @ $1.00/MTok + 567 out @ $5.00 + 8900 cached @ $0.10.
        assert result.cost_usd == Decimal("0.004959")

    def test_records_the_stop_reason(self, result):
        assert result.stop_reason == "end_turn"

    def test_measures_latency(self, result):
        # A fake client returns in microseconds, so the only honest assertions
        # are the type and the floor; the value is wired, not invented.
        assert isinstance(result.latency_ms, int)
        assert result.latency_ms >= 0

    def test_records_the_params_an_eval_would_need_to_replay(self, result):
        assert result.params == {"max_tokens": MAX_TOKENS, "thinking": "off", "doc_type": "receipt"}

    def test_takes_the_first_text_block_past_any_other_block(self):
        response = make_response(
            content=[
                SimpleNamespace(type="thinking", thinking="..."),
                SimpleNamespace(type="text", text=json.dumps(PAYLOAD)),
            ]
        )
        assert extract_with(FakeAnthropic(response)).data == PAYLOAD

    def test_a_missing_cache_field_reads_as_zero(self):
        response = make_response(usage=make_usage(10, 10))
        assert extract_with(FakeAnthropic(response)).cache_read_tokens == 0

    def test_a_null_cache_field_reads_as_zero(self):
        response = make_response(usage=make_usage(10, 10, cache_read=None))
        assert extract_with(FakeAnthropic(response)).cache_read_tokens == 0


class TestRequestShape:
    @pytest.fixture
    def call(self) -> dict:
        client = FakeAnthropic(make_response())
        extract_with(client, model="claude-sonnet-5", doc_type="invoice")
        return client.messages.calls[0]

    def test_calls_the_api_once(self):
        client = FakeAnthropic(make_response())
        extract_with(client)
        assert len(client.messages.calls) == 1

    def test_sends_the_configured_model(self, call):
        assert call["model"] == "claude-sonnet-5"

    def test_sends_the_token_ceiling(self, call):
        assert call["max_tokens"] == MAX_TOKENS

    def test_sends_the_stable_guidance_as_the_system_prompt(self, call):
        assert call["system"] == SYSTEM_PROMPT

    def test_constrains_the_output_with_the_schema(self, call):
        """Structured outputs, not a forced tool call -- see the module docstring."""
        assert call["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
        assert call["output_config"]["format"]["schema"] is SCHEMA

    def test_does_not_smuggle_the_schema_through_a_tool(self, call):
        assert "tools" not in call
        assert "tool_choice" not in call

    def test_puts_only_the_volatile_part_in_the_user_turn(self, call):
        """Keeping the prefix constant is what prompt caching would need later."""
        assert len(call["messages"]) == 1
        assert call["messages"][0]["role"] == "user"
        content = call["messages"][0]["content"]
        assert "invoice" in content
        assert OCR_TEXT in content
        assert SYSTEM_PROMPT not in content

    def test_does_not_ask_for_thinking(self, call):
        """Extraction is perception; thinking would buy latency and tokens only."""
        assert "thinking" not in call


class TestPricing:
    """
    The cost figure is what this project exists to report, so it is asserted
    against values computed by hand rather than by re-running the formula.
    """

    @pytest.mark.parametrize(("model", "rates"), sorted(PRICING.items()))
    def test_each_model_bills_each_token_class_at_its_own_rate(self, model, rates):
        per_in, per_out, per_cache = rates
        # One million of each makes the expected total the rates themselves.
        usage = make_usage(1_000_000, 1_000_000, 1_000_000)
        assert price(model, usage) == Decimal(per_in) + Decimal(per_out) + Decimal(per_cache)

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("claude-haiku-4-5", "0.004959"),
            ("claude-sonnet-5", "0.009918"),
            ("claude-opus-5", "0.024795"),
        ],
    )
    def test_a_realistic_receipt_call(self, model, expected):
        # 1234 input, 567 output, 8900 cache reads, at each model's rates.
        assert price(model, make_usage(1234, 567, 8900)) == Decimal(expected)

    def test_cache_reads_are_billed_below_fresh_input(self):
        fresh = price("claude-opus-5", make_usage(100_000, 0, 0))
        cached = price("claude-opus-5", make_usage(0, 0, 100_000))
        assert cached == Decimal("0.05")
        assert fresh == Decimal("0.50")

    def test_returns_an_exact_decimal_not_a_float(self):
        assert isinstance(price("claude-haiku-4-5", make_usage(1234, 0, 0)), Decimal)

    def test_totals_stay_exact_when_accumulated(self):
        """
        Three identical documents on one invoice line. The same sum in float is
        0.0037020000000000004, which is why price() never touches one.
        """
        one = price("claude-haiku-4-5", make_usage(1234, 0, 0))
        assert one * 3 == Decimal("0.003702")
        assert sum([one, one, one]) == Decimal("0.003702")

    def test_an_unknown_model_costs_nothing_rather_than_raising(self):
        """A missing price must not fail a document that already succeeded."""
        assert price("claude-sonnet-9", make_usage(1_000_000, 1_000_000, 1_000_000)) == Decimal("0")

    def test_a_usage_without_the_cache_attribute(self):
        assert price("claude-haiku-4-5", make_usage(1_000_000, 0)) == Decimal("1.00")

    def test_a_usage_with_a_null_cache_count(self):
        usage = make_usage(1_000_000, 0, cache_read=None)
        assert price("claude-haiku-4-5", usage) == Decimal("1.00")

    def test_a_call_that_used_nothing_costs_nothing(self):
        assert price("claude-haiku-4-5", make_usage(0, 0, 0)) == Decimal("0")

    def test_sub_cent_calls_are_not_rounded_away(self):
        """Per-document costs are tiny; rounding here would report every one as free."""
        assert price("claude-haiku-4-5", make_usage(1, 0, 0)) == Decimal("0.000001")


class TestTransientFailures:
    """
    Anything the worker should retry. ExtractionRefused subclasses
    ExtractionError, so each case also asserts it is *not* the refused subtype
    -- a bare `pytest.raises(ExtractionError)` would pass either way.
    """

    def _failing(self, exc: Exception) -> ExtractionError:
        with pytest.raises(ExtractionError) as excinfo:
            extract_with(FakeAnthropic(raises=exc))
        assert not isinstance(excinfo.value, ExtractionRefused)
        return excinfo.value

    def test_a_rate_limit_is_retryable(self):
        """
        429 is a 4xx, so this only works because RateLimitError is caught
        before the APIStatusError clause that condemns 4xx.
        """
        raised = self._failing(status_error(anthropic.RateLimitError, 429))
        assert "rate limited" in str(raised)

    @pytest.mark.parametrize("status_code", [500, 502, 503, 529])
    def test_server_errors_are_retryable(self, status_code):
        raised = self._failing(status_error(anthropic.APIStatusError, status_code))
        assert str(status_code) in str(raised)

    def test_a_connection_failure_is_retryable(self):
        exc = anthropic.APIConnectionError(message="connection refused", request=REQUEST)
        assert "connection failed" in str(self._failing(exc))

    def test_a_timeout_is_retryable(self):
        """APITimeoutError is an APIConnectionError, so the same clause covers it."""
        self._failing(anthropic.APITimeoutError(request=REQUEST))

    def test_a_response_with_no_text_block_is_retryable(self):
        response = make_response(content=[SimpleNamespace(type="thinking", thinking="...")])
        with pytest.raises(ExtractionError) as excinfo:
            extract_with(FakeAnthropic(response))
        assert not isinstance(excinfo.value, ExtractionRefused)
        assert "no text block" in str(excinfo.value)

    def test_an_empty_content_list_is_retryable(self):
        with pytest.raises(ExtractionError, match="no text block"):
            extract_with(FakeAnthropic(make_response(content=[])))

    def test_unparseable_output_is_retryable(self):
        """Structured outputs make this near-impossible, hence transient, not permanent."""
        response = make_response(content=[SimpleNamespace(type="text", text="{not json")])
        with pytest.raises(ExtractionError) as excinfo:
            extract_with(FakeAnthropic(response))
        assert not isinstance(excinfo.value, ExtractionRefused)
        assert "not valid JSON" in str(excinfo.value)


class TestPermanentFailures:
    """Anything retrying the same bytes cannot fix."""

    @pytest.mark.parametrize("status_code", [400, 401, 403, 404, 413, 422])
    def test_rejected_requests_are_permanent(self, status_code):
        exc = status_error(anthropic.APIStatusError, status_code)
        with pytest.raises(ExtractionRefused, match=f"rejected \\({status_code}\\)"):
            extract_with(FakeAnthropic(raises=exc))

    def test_a_refusal_is_permanent(self):
        response = make_response(
            stop_reason="refusal",
            stop_details=SimpleNamespace(type="refusal", category="cyber", explanation="no"),
        )
        with pytest.raises(ExtractionRefused, match="category=cyber"):
            extract_with(FakeAnthropic(response))

    def test_a_refusal_without_details_still_reports(self):
        """stop_details is null for every stop_reason but refusal, and may be null there too."""
        with pytest.raises(ExtractionRefused, match="category=None"):
            extract_with(FakeAnthropic(make_response(stop_reason="refusal", stop_details=None)))

    def test_truncated_output_is_permanent(self):
        """The JSON is unparseable and the next attempt truncates in the same place."""
        response = make_response(
            stop_reason="max_tokens",
            content=[SimpleNamespace(type="text", text='{"merchant": "CAFE LU')],
        )
        with pytest.raises(ExtractionRefused, match=str(MAX_TOKENS)):
            extract_with(FakeAnthropic(response))

    def test_a_refusal_is_checked_before_the_payload(self):
        """A refusal carries no JSON; reading content first would mask the reason."""
        response = make_response(stop_reason="refusal", content=[])
        with pytest.raises(ExtractionRefused, match="declined"):
            extract_with(FakeAnthropic(response))


class TestDatedModelIds:
    """
    The API answers with a dated id where the request named an alias, and the
    dated one is what lands on extractions.model_used. If that cannot be
    priced, re-pricing a stored row returns $0 -- which is worse than raising,
    because it still sums into a cost report that then reads as free.
    """

    def test_an_alias_resolves_to_itself(self):
        assert pricing_key("claude-haiku-4-5") == "claude-haiku-4-5"

    def test_a_dated_id_resolves_to_its_alias(self):
        assert pricing_key("claude-haiku-4-5-20251001") == "claude-haiku-4-5"

    def test_every_pricing_entry_survives_a_date_suffix(self):
        for alias in PRICING:
            assert pricing_key(f"{alias}-20260101") == alias

    def test_a_dated_id_prices_the_same_as_its_alias(self):
        usage = SimpleNamespace(
            input_tokens=1981, output_tokens=150, cache_read_input_tokens=0
        )
        assert price("claude-haiku-4-5-20251001", usage) == price(
            "claude-haiku-4-5", usage
        )

    def test_the_figure_matches_the_observed_live_call(self):
        """1,981 in and 150 out at $1/$5 per MTok, as actually billed."""
        usage = SimpleNamespace(
            input_tokens=1981, output_tokens=150, cache_read_input_tokens=0
        )
        assert price("claude-haiku-4-5-20251001", usage) == Decimal("0.002731")

    def test_a_different_model_sharing_a_prefix_is_not_resolved(self):
        """A hypothetical -mini is a different model; it must miss, not borrow."""
        assert pricing_key("claude-haiku-4-5-mini") is None

    def test_a_malformed_suffix_is_not_stripped(self):
        assert pricing_key("claude-haiku-4-5-2025") is None
        assert pricing_key("claude-haiku-4-5-202510011") is None

    def test_an_unknown_model_still_prices_at_zero_rather_than_raising(self):
        usage = SimpleNamespace(
            input_tokens=10, output_tokens=10, cache_read_input_tokens=0
        )
        assert price("some-other-provider/model", usage) == Decimal("0")
