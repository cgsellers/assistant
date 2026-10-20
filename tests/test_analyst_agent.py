"""
The analyst loop.

Everything here uses a fake client. What is being tested is the loop and the
boundary it enforces -- that the agent can read the ledger and cannot touch
it -- not whether a model writes good prose.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from analyst import tools
from analyst.agent import TOOL_SPECS, Analyst, AnalystError, run_tool
from core import repository

from .conftest import TINY_PNG


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _use(name, arguments, call_id="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=arguments, id=call_id)


def _response(content, stop_reason="end_turn", model="claude-sonnet-5"):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model=model,
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20, cache_read_input_tokens=0
        ),
    )


class FakeClient:
    """Replays a scripted sequence of responses and records what it was sent."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        # A copy: the loop appends to the same list it hands over, so storing
        # the reference would record the end state against every turn.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self._responses:
            raise AssertionError("the loop asked for more turns than were scripted")
        return self._responses.pop(0)


@pytest.fixture
def ledger(session):
    def _add(amount, *, who="MERCADONA S.A.", when=date(2026, 3, 1), currency="EUR"):
        document, _ = repository.ingest(
            session,
            data=TINY_PNG + str(amount).encode(),
            mime_type="image/png",
            original_filename="r.png",
        )
        extraction = repository.record_extraction(
            session,
            document_id=document.id,
            doc_type="receipt",
            schema_version=1,
            model_used="fake",
            raw_json={"total": amount},
        )
        return repository.record_transaction(
            session,
            extraction=extraction,
            values={
                "source_type": "receipt",
                "direction": "out",
                "amount": Decimal(amount),
                "currency": currency,
                "occurred_on": when,
                "counterparty": who,
            },
        )

    return _add


class TestTheToolsAreAllReadOnly:
    """
    The approval gate is the absence of a capability, not a rule the model is
    asked to follow. If a write tool ever appears, this test is where it
    should have to be argued for.
    """

    def test_no_tool_name_suggests_a_write(self):
        forbidden = ("create", "update", "delete", "set", "write", "apply", "commit")
        for spec in TOOL_SPECS:
            assert not any(word in spec["name"] for word in forbidden), spec["name"]

    def test_running_every_tool_changes_nothing(self, session, ledger):
        from core.models import Transaction

        row = ledger("10.00")
        before = {
            (t.id, t.amount, t.is_current, t.category, t.user_verified)
            for t in session.query(Transaction).all()
        }

        run_tool(session, "ledger_summary", {})
        run_tool(session, "total_spend", {})
        run_tool(session, "list_transactions", {})
        run_tool(session, "spend_by_counterparty", {})
        run_tool(session, "spend_by_month", {})
        run_tool(session, "get_source", {"transaction_id": row.id})

        session.expire_all()
        after = {
            (t.id, t.amount, t.is_current, t.category, t.user_verified)
            for t in session.query(Transaction).all()
        }
        assert before == after

    def test_an_unknown_tool_is_refused(self, session):
        with pytest.raises(KeyError, match="no such tool"):
            run_tool(session, "delete_everything", {})

    def test_the_system_prompt_forbids_arithmetic(self):
        from analyst.agent import SYSTEM_PROMPT

        assert "Never calculate" in SYSTEM_PROMPT
        assert "Never add different currencies" in SYSTEM_PROMPT


class TestDispatch:
    def test_dates_arrive_as_strings_and_are_parsed(self, session, ledger):
        ledger("10.00", when=date(2026, 3, 1))
        ledger("99.00", when=date(2026, 9, 1))

        result = run_tool(
            session, "total_spend", {"start": "2026-01-01", "end": "2026-06-30"}
        )

        assert result.payload["totals"][0]["total"] == "10.00"

    def test_a_minimum_amount_arrives_as_a_string_and_stays_decimal(
        self, session, ledger
    ):
        ledger("10.00")
        ledger("100.00")

        rows = run_tool(session, "list_transactions", {"min_amount": "50.00"})

        assert len(rows.payload) == 1

    def test_results_are_json_serialisable(self, session, ledger):
        """They go into a tool_result block, so they have to survive dumps()."""
        row = ledger("10.00")
        for name, args in [
            ("ledger_summary", {}),
            ("total_spend", {}),
            ("list_transactions", {}),
            ("spend_by_counterparty", {}),
            ("spend_by_month", {}),
            ("get_source", {"transaction_id": row.id}),
        ]:
            json.dumps(run_tool(session, name, args).payload, default=str)


class TestTheLoop:
    def test_a_plain_answer_comes_straight_back(self, session):
        analyst = Analyst(client=FakeClient(_response([_text("Nothing to report.")])))

        answer = analyst.ask(session, "how am I doing")

        assert answer.text == "Nothing to report."
        assert answer.turns == 1
        assert answer.tool_calls == []

    def test_a_tool_call_is_run_and_fed_back(self, session, ledger):
        ledger("10.00")
        client = FakeClient(
            _response([_use("total_spend", {})], stop_reason="tool_use"),
            _response([_text("You spent 10.00 EUR.")]),
        )

        answer = Analyst(client=client).ask(session, "what did I spend")

        assert answer.text == "You spent 10.00 EUR."
        assert answer.tool_calls == ["total_spend"]
        assert answer.turns == 2
        # The result really went back as a tool_result block.
        second = client.requests[1]["messages"][-1]
        assert second["content"][0]["type"] == "tool_result"
        assert "10.00" in second["content"][0]["content"]

    def test_several_tools_in_one_turn_all_run(self, session, ledger):
        ledger("10.00")
        client = FakeClient(
            _response(
                [
                    _use("ledger_summary", {}, "a"),
                    _use("total_spend", {}, "b"),
                ],
                stop_reason="tool_use",
            ),
            _response([_text("done")]),
        )

        answer = Analyst(client=client).ask(session, "q")

        assert answer.tool_calls == ["ledger_summary", "total_spend"]
        # Both results come back in one user message, as the API requires.
        results = client.requests[1]["messages"][-1]["content"]
        assert len(results) == 2
        assert {r["tool_use_id"] for r in results} == {"a", "b"}

    def test_tokens_and_cost_accumulate_across_turns(self, session, ledger):
        ledger("10.00")
        client = FakeClient(
            _response([_use("total_spend", {})], stop_reason="tool_use"),
            _response([_text("done")]),
        )

        answer = Analyst(client=client).ask(session, "q")

        assert answer.input_tokens == 200
        assert answer.output_tokens == 40
        assert answer.cost_usd > 0
        assert isinstance(answer.cost_usd, Decimal)

    def test_a_runaway_conversation_is_stopped(self, session):
        forever = [
            _response([_use("ledger_summary", {})], stop_reason="tool_use")
            for _ in range(10)
        ]
        analyst = Analyst(client=FakeClient(*forever), max_turns=3)

        with pytest.raises(AnalystError, match="gave up after 3 turns"):
            analyst.ask(session, "q")

    def test_a_refusal_is_raised_rather_than_returned_as_an_answer(self, session):
        analyst = Analyst(client=FakeClient(_response([], stop_reason="refusal")))

        with pytest.raises(AnalystError, match="declined"):
            analyst.ask(session, "q")

    def test_a_failing_tool_is_reported_to_the_model_not_swallowed(self, session):
        """Dropping it would leave the model waiting for a result forever."""
        # A date the model invented in the wrong format: the tool raises, and
        # the model needs to hear about it.
        client = FakeClient(
            _response(
                [_use("total_spend", {"start": "March 2026"})], stop_reason="tool_use"
            ),
            _response([_text("I could not read that date.")]),
        )

        answer = Analyst(client=client).ask(session, "q")

        result = client.requests[1]["messages"][-1]["content"][0]
        assert result["is_error"] is True
        assert answer.text.startswith("I could not read")

    def test_no_key_is_needed_until_a_call_is_made(self, session, monkeypatch):
        monkeypatch.setattr(
            "analyst.agent.get_settings", lambda: SimpleNamespace(anthropic_api_key=None)
        )
        analyst = Analyst()  # constructing is free

        with pytest.raises(AnalystError, match="ANTHROPIC_API_KEY"):
            analyst.ask(session, "q")


class TestCitations:
    def test_sources_are_gathered_from_results_not_from_the_prose(
        self, session, ledger
    ):
        """
        The model can misreport a citation. It cannot invent one here,
        because the ids come off the tool results rather than the answer.
        """
        a = ledger("10.00")
        b = ledger("20.00")
        client = FakeClient(
            _response([_use("total_spend", {})], stop_reason="tool_use"),
            _response([_text("Two receipts, document 00000000.")]),
        )

        answer = Analyst(client=client).ask(session, "q")

        assert set(answer.sources) == {a.document_id, b.document_id}
        assert "00000000" not in answer.sources

    def test_a_drill_into_one_source_is_also_cited(self, session, ledger):
        row = ledger("10.00")
        client = FakeClient(
            _response(
                [_use("get_source", {"transaction_id": row.id})], stop_reason="tool_use"
            ),
            _response([_text("From one receipt.")]),
        )

        answer = Analyst(client=client).ask(session, "q")

        assert answer.sources == [row.document_id]

    def test_an_answer_with_no_tools_cites_nothing(self, session):
        answer = Analyst(client=FakeClient(_response([_text("Hello.")]))).ask(session, "hi")
        assert answer.sources == []


class TestWhatIsSent:
    def test_every_tool_is_offered(self, session):
        client = FakeClient(_response([_text("ok")]))
        Analyst(client=client).ask(session, "q")

        offered = {t["name"] for t in client.requests[0]["tools"]}
        assert offered == {s["name"] for s in TOOL_SPECS}

    def test_tools_are_strict_so_arguments_are_schema_valid(self, session):
        """They go straight into a database query."""
        client = FakeClient(_response([_text("ok")]))
        Analyst(client=client).ask(session, "q")

        assert all(t["strict"] for t in client.requests[0]["tools"])

    def test_the_question_is_the_whole_user_turn(self, session):
        client = FakeClient(_response([_text("ok")]))
        Analyst(client=client).ask(session, "what did I spend in March")

        assert client.requests[0]["messages"] == [
            {"role": "user", "content": "what did I spend in March"}
        ]


class TestAmountFormatting:
    def test_the_same_number_has_one_spelling(self):
        """`Numeric(14, 4)` reads back 10.0000 where memory holds 10.00."""
        assert tools.amount_str(Decimal("10.00")) == "10.00"
        assert tools.amount_str(Decimal("10.0000")) == "10.00"

    def test_genuine_extra_precision_survives(self):
        assert tools.amount_str(Decimal("10.123")) == "10.123"

    def test_whole_numbers_still_read_as_money(self):
        assert tools.amount_str(Decimal("10")) == "10.00"

    def test_negatives_are_not_mangled(self):
        assert tools.amount_str(Decimal("-3.5")) == "-3.50"


class TestCitationsCannotBeInjected:
    """
    Citations used to be recovered by walking tool payloads for keys named
    `document_id` or `sources`. `get_source` embeds `extraction.raw_json`,
    and a document type may declare any lower_snake_case field name it
    likes -- so content read off an uploaded page could put entries into the
    provenance list. That is the one property this agent exists to
    guarantee, and it failed on the only input the system treats as
    untrusted.
    """

    def test_extracted_content_cannot_reach_the_sources(self, session, ledger):
        from analyst.agent import run_tool

        row = ledger("10.00")
        # A document type whose fields are named to collide with the old
        # walker, holding values a hostile document could have carried.
        extraction = session.get(
            __import__("core.models", fromlist=["Extraction"]).Extraction,
            row.source_extraction_id,
        )
        extraction.raw_json = {
            "total": "10.00",
            "document_id": "INJECTED-DOCUMENT",
            "sources": ["INJECTED-LIST"],
        }
        session.commit()

        result = run_tool(session, "get_source", {"transaction_id": row.id})

        # The payload still carries the raw extraction, because a reader
        # should see what was on the page...
        assert result.payload["extraction"]["raw_json"]["document_id"] == (
            "INJECTED-DOCUMENT"
        )
        # ...but provenance comes off the row, not out of the payload.
        assert result.document_ids == (row.document_id,)

    def test_the_loop_only_ever_cites_what_a_tool_declared(self, session, ledger):
        row = ledger("10.00")
        client = FakeClient(
            _response(
                [_use("get_source", {"transaction_id": row.id})], stop_reason="tool_use"
            ),
            _response([_text("From document INVENTED-BY-THE-MODEL.")]),
        )

        answer = Analyst(client=client).ask(session, "q")

        assert answer.sources == [row.document_id]
        assert "INVENTED-BY-THE-MODEL" not in answer.sources

    def test_a_tool_that_declares_no_sources_adds_none(self, session, ledger):
        """Orientation calls describe the ledger; they cite no document."""
        from analyst.agent import run_tool

        ledger("10.00")
        assert run_tool(session, "ledger_summary", {}).document_ids == ()

    def test_every_tool_returns_the_typed_result(self, session, ledger):
        """So a new tool has to state its sources rather than hope."""
        from analyst.agent import TOOL_SPECS, ToolResult, run_tool

        row = ledger("10.00")
        args = {"get_source": {"transaction_id": row.id}}
        for spec in TOOL_SPECS:
            result = run_tool(session, spec["name"], args.get(spec["name"], {}))
            assert isinstance(result, ToolResult), spec["name"]
            assert isinstance(result.document_ids, tuple), spec["name"]
