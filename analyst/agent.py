"""
The analyst agent: a model that answers questions about the ledger.

Its one job is to choose a tool and explain the result. It does not add
numbers up. A model asked "what did I spend at Mercadona" that answers from
figures in its context will occasionally be wrong, and wrong invisibly --
the answer reads exactly like a correct one. So arithmetic happens in
`analyst.tools`, under test, and the model's contribution is knowing which
question to ask and how to say the answer.

It also cannot change anything. Every tool is a read, so "requires human
approval" is not a rule the model is asked to honour but a capability it was
never given. The agent can recommend a category for a transaction; applying
one is a separate call a person makes.

The loop is written out rather than delegated to a helper. It is forty lines,
it has no beta dependency, and being able to read exactly what is sent and
what comes back is worth more here than the lines it would save -- this is
the part of the system a reviewer will want to audit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import anthropic
from sqlalchemy.orm import Session

from analyst import tools
from core.config import get_settings
from llm.anthropic import price

log = logging.getLogger("analyst")

DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096
#: Enough for orient, query, maybe drill into a source, answer. A question
#: that cannot be answered in this many is one the tools do not cover.
MAX_TURNS = 6

SYSTEM_PROMPT = """\
You answer questions about a small business's ledger of transactions.

The rules you work under:

- **Never calculate.** Every figure in your answer must come from a tool
  result, copied exactly. Do not add, subtract, average or project. If a
  number you need is not in a tool result, call another tool or say you
  cannot answer it.
- **Never add different currencies together.** When a result is split by
  currency, report it split. They are not convertible here.
- **Cite your sources.** Tool results carry document ids. Name how many
  documents an answer rests on, and give ids when the user asks where a
  figure came from.
- **Say when the ledger is silent.** A field a document did not carry is
  null, and "the receipt does not say" is a complete and useful answer. Do
  not fill gaps with what is likely.
- **You cannot change anything.** Every tool is read-only. If the user asks
  you to categorise, correct or delete something, explain what you would
  change and tell them it needs their confirmation to apply.

Start with `ledger_summary` if you do not know what the ledger contains --
its date range and currencies tell you whether a question is even
answerable. Be brief and concrete.\
"""


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


_WINDOW = {
    "start": {"type": "string", "description": "Inclusive start date, YYYY-MM-DD."},
    "end": {"type": "string", "description": "Inclusive end date, YYYY-MM-DD."},
}


def _spec(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        # strict: the arguments are turned straight into a database query, so
        # a hallucinated extra key should be the API's problem, not ours.
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": [],
            "additionalProperties": False,
        },
    }


TOOL_SPECS: list[dict] = [
    _spec(
        "ledger_summary",
        "How many transactions the ledger holds, the date range they cover, and "
        "which currencies appear. Call this first when you do not know what is "
        "there.",
        {},
    ),
    _spec(
        "total_spend",
        "Total spent in a period, split by currency, with the tax total and the ids of "
        "the documents behind it.",
        {
            **_WINDOW,
            "counterparty": {
                "type": "string",
                "description": "Case-insensitive substring of the business name.",
            },
            "category": {"type": "string", "description": "Exact category name."},
        },
    ),
    _spec(
        "list_transactions",
        "Individual transactions, newest first, each with its document id.",
        {
            **_WINDOW,
            "counterparty": {"type": "string", "description": "Name substring."},
            "min_amount": {
                "type": "string",
                "description": "Only rows at or above this amount, e.g. '50.00'.",
            },
            "limit": {"type": "integer", "description": "How many rows, default 20."},
        },
    ),
    _spec(
        "spend_by_counterparty",
        "Who the money went to, largest first, split by currency.",
        {**_WINDOW, "limit": {"type": "integer", "description": "How many, default 10."}},
    ),
    _spec(
        "spend_by_month",
        "Monthly totals, oldest first. Transactions with no readable date are "
        "reported under 'undated'.",
        _WINDOW,
    ),
    _spec(
        "get_source",
        "The evidence behind one transaction: the document it came from, the "
        "model that read it, and the raw extracted values.",
        {
            "transaction_id": {
                "type": "string",
                "description": "The transaction_id from another tool's result.",
            }
        },
    ),
]


def run_tool(session: Session, name: str, arguments: dict) -> ToolResult:
    """
    Dispatch one tool call. Every branch is a read, and every branch says
    which documents its answer came from.
    """
    if name == "ledger_summary":
        # Orientation only: counts and ranges, nothing traceable to a page.
        return ToolResult(tools.ledger_summary(session))

    if name == "total_spend":
        totals = tools.total_spend(
            session,
            start=_date(arguments.get("start")),
            end=_date(arguments.get("end")),
            counterparty=arguments.get("counterparty"),
            category=arguments.get("category"),
        )
        return ToolResult(totals.as_dict(), tuple(totals.document_ids))

    if name == "list_transactions":
        raw_min = arguments.get("min_amount")
        rows = tools.list_transactions(
            session,
            start=_date(arguments.get("start")),
            end=_date(arguments.get("end")),
            counterparty=arguments.get("counterparty"),
            min_amount=Decimal(raw_min) if raw_min else None,
            limit=int(arguments.get("limit") or tools.DEFAULT_LIMIT),
        )
        return ToolResult(rows, tuple(r["document_id"] for r in rows))

    if name == "spend_by_counterparty":
        return ToolResult(
            tools.spend_by_counterparty(
                session,
                start=_date(arguments.get("start")),
                end=_date(arguments.get("end")),
                limit=int(arguments.get("limit") or 10),
            )
        )

    if name == "spend_by_month":
        return ToolResult(
            tools.spend_by_month(
                session,
                start=_date(arguments.get("start")),
                end=_date(arguments.get("end")),
            )
        )

    if name == "get_source":
        source = tools.get_source(session, arguments.get("transaction_id", ""))
        if source is None:
            return ToolResult(None)
        # The id off the row, not out of the payload -- the payload carries
        # extracted document content and must never be a source of truth
        # about provenance.
        return ToolResult(source, (source["document_id"],))

    raise KeyError(f"no such tool: {name}")


@dataclass(frozen=True)
class ToolResult:
    """
    What a tool hands back: a payload for the model, and the documents the
    answer rests on.

    The two are separate on purpose. Citations used to be recovered by
    walking the payload for keys named `document_id` or `sources`, which
    worked until a payload contained one by accident -- `get_source` embeds
    `extraction.raw_json`, and a document type may declare any
    lower_snake_case field name it likes. Content read off an uploaded page
    could therefore put entries into the provenance list, which is the one
    property this agent exists to guarantee.

    Now each tool states its sources explicitly, and adding a tool means
    deciding what they are rather than hoping the walker finds them.
    """

    payload: Any
    document_ids: tuple[str, ...] = ()


@dataclass
class Answer:
    text: str
    #: Documents the answer rests on, gathered from the tool results rather
    #: than from anything the model said. The model can misreport a citation;
    #: it cannot invent one here.
    sources: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    stop_reason: str | None = None


class AnalystError(Exception):
    """The question could not be answered."""


class Analyst:
    """A read-only conversation with the ledger."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        client: anthropic.Anthropic | None = None,
        max_turns: int = MAX_TURNS,
    ):
        self.model = model
        self.max_turns = max_turns
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            key = get_settings().anthropic_api_key
            if not key:
                raise AnalystError("ANTHROPIC_API_KEY is not set")
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def ask(self, session: Session, question: str) -> Answer:
        """
        Answer one question, running tools until the model stops asking.

        History is local to the call. A persistent chat is a thin layer on
        top of this; what has to be right first is that one question produces
        one auditable set of tool calls.
        """
        messages: list[dict] = [{"role": "user", "content": question}]
        answer = Answer(text="")
        sources: set[str] = set()

        for turn in range(1, self.max_turns + 1):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOL_SPECS,
                messages=messages,
            )
            answer.turns = turn
            answer.input_tokens += response.usage.input_tokens
            answer.output_tokens += response.usage.output_tokens
            answer.cost_usd += price(self.model, response.usage)
            answer.stop_reason = response.stop_reason

            if response.stop_reason == "refusal":
                raise AnalystError("the model declined to answer")

            messages.append({"role": "assistant", "content": response.content})

            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                answer.text = "".join(
                    b.text for b in response.content if b.type == "text"
                )
                answer.sources = sorted(sources)
                return answer

            results = []
            for call in calls:
                answer.tool_calls.append(call.name)
                try:
                    result = run_tool(session, call.name, dict(call.input))
                    sources.update(result.document_ids)
                    content, is_error = json.dumps(result.payload, default=str), False
                except Exception as exc:  # noqa: BLE001 - reported to the model
                    # The model gets the failure and can try something else.
                    # Dropping the result instead would leave it waiting for
                    # an answer that never comes.
                    log.warning("tool %s failed: %s", call.name, exc)
                    content, is_error = f"{type(exc).__name__}: {exc}", True
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": content,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})

        raise AnalystError(
            f"gave up after {self.max_turns} turns without an answer; the question "
            f"may need a tool that does not exist"
        )
