"""
What the model calls have cost, read back from `extractions`.

Every extraction row has carried tokens, cost and latency since phase 2, and
nothing read them. This is what makes "every model call is recorded with
tokens, cost and latency" something you can see rather than take on trust.

Three numbers are easy to confuse, so they are kept apart:

  * **total** -- everything billed, including failed attempts and superseded
    extractions. This is what the invoice says.
  * **wasted** -- the part of the total spent on calls that produced nothing
    usable: refusals, truncations, unparseable output. Only knowable because
    those calls are now recorded at all.
  * **per document** -- total divided by distinct documents. Higher than the
    cost of one successful call, by exactly the retries and failures, which
    is the honest answer to "what does processing a receipt cost".

Authoring trials are billed too, but they are not production traffic, so
they are reported beside the total rather than inside it.

**Scope:** extraction only. The analyst agent reports its cost on each
`Answer` but does not persist it, so its spend is not here.

**Whose:** the session's (core/scoping.py). Through `GET /costs` a client sees
their own spend and staff see everyone's, split by organisation; the command
line reports every organisation, or one with `--client`.

Run it with:  uv run python -m core.costs --since 2026-10-01
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import Extraction, Organisation

ZERO = Decimal("0")


@dataclass(frozen=True)
class Slice:
    """Spend for one model, or one document type."""

    key: str
    calls: int
    total_usd: Decimal

    def as_dict(self) -> dict:
        return {"key": self.key, "calls": self.calls, "total_usd": _usd(self.total_usd)}


@dataclass(frozen=True)
class CostReport:
    since: date | None
    until: date | None
    calls: int = 0
    failed_calls: int = 0
    documents: int = 0
    retried_documents: int = 0
    total_usd: Decimal = ZERO
    wasted_usd: Decimal = ZERO
    trial_calls: int = 0
    trial_usd: Decimal = ZERO
    #: Calls that used tokens but priced at zero -- a model missing from the
    #: price table. Counted rather than silently summed as free.
    unpriced_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    by_model: list[Slice] = field(default_factory=list)
    by_doc_type: list[Slice] = field(default_factory=list)
    #: Spend per client -- what billing a client would start from. A client's
    #: own report has one entry: the session's scope sees only theirs.
    by_organisation: list[Slice] = field(default_factory=list)

    @property
    def per_document_usd(self) -> Decimal:
        return self.total_usd / self.documents if self.documents else ZERO

    @property
    def per_call_usd(self) -> Decimal:
        return self.total_usd / self.calls if self.calls else ZERO

    def as_dict(self) -> dict:
        return {
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "calls": self.calls,
            "failed_calls": self.failed_calls,
            "documents": self.documents,
            "retried_documents": self.retried_documents,
            "total_usd": _usd(self.total_usd),
            "wasted_usd": _usd(self.wasted_usd),
            "per_document_usd": _usd(self.per_document_usd),
            "per_call_usd": _usd(self.per_call_usd),
            "trial_calls": self.trial_calls,
            "trial_usd": _usd(self.trial_usd),
            "unpriced_calls": self.unpriced_calls,
            "tokens": {
                "input": self.input_tokens,
                "output": self.output_tokens,
                "cache_read": self.cache_read_tokens,
            },
            "latency_ms": {"p50": self.latency_p50_ms, "p95": self.latency_p95_ms},
            "by_model": [s.as_dict() for s in self.by_model],
            "by_doc_type": [s.as_dict() for s in self.by_doc_type],
            "by_organisation": [s.as_dict() for s in self.by_organisation],
        }


def _usd(value: Decimal) -> str:
    """
    Six places. A single extraction is a fraction of a cent, so the two-place
    rule in core.money would round most of these to zero -- the same reason
    `cost_usd` is Numeric(12, 6).
    """
    return str(value.quantize(Decimal("0.000001")))


def _percentile(sorted_values: list[int], fraction: float) -> int | None:
    """Nearest-rank. Small samples make anything fancier spurious precision."""
    if not sorted_values:
        return None
    # ceil, not round. Nearest-rank is defined as ceil(p * n), and Python's
    # round() goes half-to-even -- round(2.5) is 2 -- which put the median of
    # five values at the second-smallest. A test caught it.
    rank = max(1, math.ceil(fraction * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def _slices(rows: list[Extraction], key) -> list[Slice]:
    groups: dict[str, list[Extraction]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return sorted(
        (
            Slice(
                key=name,
                calls=len(members),
                total_usd=sum((r.cost_usd or ZERO for r in members), ZERO),
            )
            for name, members in groups.items()
        ),
        key=lambda s: s.total_usd,
        reverse=True,
    )


def cost_report(
    session: Session, *, since: date | None = None, until: date | None = None
) -> CostReport:
    """Spend between two dates, inclusive. Either bound may be open."""
    stmt = select(Extraction)
    if since is not None:
        stmt = stmt.where(Extraction.created_at >= datetime.combine(since, time.min))
    if until is not None:
        stmt = stmt.where(Extraction.created_at <= datetime.combine(until, time.max))

    everything = list(session.scalars(stmt))
    names = dict(session.execute(select(Organisation.id, Organisation.name)).all())
    trials = [r for r in everything if r.is_trial]
    rows = [r for r in everything if not r.is_trial]

    if not rows and not trials:
        return CostReport(since=since, until=until)

    # A failed attempt is a billed call with no answer -- see
    # repository.record_failed_attempt.
    failed = [r for r in rows if r.raw_json is None]
    attempts: dict[str, int] = {}
    for row in rows:
        attempts[row.document_id] = attempts.get(row.document_id, 0) + 1
    latencies = sorted(r.latency_ms for r in rows if r.latency_ms is not None)

    return CostReport(
        since=since,
        until=until,
        calls=len(rows),
        failed_calls=len(failed),
        documents=len(attempts),
        retried_documents=sum(1 for n in attempts.values() if n > 1),
        total_usd=sum((r.cost_usd or ZERO for r in rows), ZERO),
        wasted_usd=sum((r.cost_usd or ZERO for r in failed), ZERO),
        trial_calls=len(trials),
        trial_usd=sum((r.cost_usd or ZERO for r in trials), ZERO),
        unpriced_calls=sum(
            1 for r in rows if (r.input_tokens or 0) > 0 and not r.cost_usd
        ),
        input_tokens=sum(r.input_tokens or 0 for r in rows),
        output_tokens=sum(r.output_tokens or 0 for r in rows),
        cache_read_tokens=sum(r.cache_read_tokens or 0 for r in rows),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        by_model=_slices(rows, lambda r: r.model_used),
        by_doc_type=_slices(rows, lambda r: r.doc_type),
        by_organisation=_slices(rows, lambda r: names.get(r.organisation_id, r.organisation_id)),
    )


def format_report(report: CostReport) -> str:
    if not report.calls and not report.trial_calls:
        return "no model calls recorded in this window"

    window = (
        f"{report.since.isoformat() if report.since else 'the beginning'} to "
        f"{report.until.isoformat() if report.until else 'now'}"
    )
    lines = [
        f"Extraction spend, {window}",
        "",
        f"  total            ${_usd(report.total_usd)}   over {report.calls} call(s)",
        f"  per document     ${_usd(report.per_document_usd)}   "
        f"({report.documents} document(s), {report.retried_documents} needed more than one call)",
        f"  per call         ${_usd(report.per_call_usd)}",
        f"  wasted           ${_usd(report.wasted_usd)}   "
        f"on {report.failed_calls} call(s) that produced nothing usable",
    ]
    if report.trial_calls:
        lines.append(
            f"  authoring trials ${_usd(report.trial_usd)}   "
            f"over {report.trial_calls} call(s), not included above"
        )
    if report.unpriced_calls:
        lines.append(
            f"  ! {report.unpriced_calls} call(s) used tokens but priced at zero "
            "-- a model missing from llm.anthropic.PRICING. The total is low."
        )
    lines += [
        "",
        f"  tokens           {report.input_tokens:,} in · {report.output_tokens:,} out"
        f" · {report.cache_read_tokens:,} cache read",
    ]
    if report.latency_p50_ms is not None:
        lines.append(
            f"  latency          p50 {report.latency_p50_ms}ms · p95 {report.latency_p95_ms}ms"
        )
    for title, slices in (
        ("by organisation", report.by_organisation),
        ("by model", report.by_model),
        ("by type", report.by_doc_type),
    ):
        lines += ["", f"  {title}"]
        lines += [
            f"    {s.key:<32} {s.calls:>5} call(s)   ${_usd(s.total_usd)}" for s in slices
        ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report extraction spend")
    parser.add_argument("--since", type=date.fromisoformat, default=None)
    parser.add_argument("--until", type=date.fromisoformat, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--client", metavar="SLUG", help="one organisation's spend only")
    args = parser.parse_args()

    from core.db import SessionLocal
    from core.scoping import Scope, set_scope

    with SessionLocal() as session:
        # An operator's command: every organisation's spend, or one client's.
        if args.client:
            from core import tenancy

            organisation = tenancy.by_slug(session, args.client)
            if organisation is None:
                raise SystemExit(f"no organisation called {args.client!r}")
            set_scope(session, Scope.of(organisation.id, f"core.costs --client {args.client}"))
        else:
            set_scope(session, Scope.everyone("core.costs"))
        report = cost_report(session, since=args.since, until=args.until)
    print(json.dumps(report.as_dict(), indent=2) if args.json else format_report(report))


if __name__ == "__main__":
    main()

