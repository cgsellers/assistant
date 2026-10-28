"""
A daily cap on what Ledgerline spends on model calls, across every
organisation (MAX_DAILY_SPEND_USD; none unless it is set).

A guard for the wallet, for when the API is reachable by people the owner
does not know: a public demo, a client whose system loops on uploads. Spend
is what the cost report counts -- every extraction's cost, failed attempts
and authoring trials included, since all of those were billed -- from
midnight UTC.

Two places consult it. An upload that would need reading is refused with 429
once today's spend reaches the cap; a document already in Ledgerline still
answers, since it costs nothing. And the worker, as a backstop for documents
queued before the cap was reached, puts their reading off until the next UTC
day rather than make the call.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.models import Extraction, utcnow
from core.scoping import Scope, set_scope


def start_of_day(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return datetime.combine(now.date(), time.min, tzinfo=UTC)


def tomorrow(now: datetime | None = None) -> datetime:
    """When the cap resets: the next midnight UTC."""
    return start_of_day(now) + timedelta(days=1)


def spent_today(session: Session) -> Decimal:
    """
    Every organisation's spend since midnight UTC. Counted in a session of its
    own that sees every organisation: the cap is about the whole bill, and the
    caller's session -- a client's request -- sees only its own.
    """
    with Session(bind=session.get_bind()) as everyone:
        set_scope(everyone, Scope.everyone("core.spend: the daily cap"))
        total = everyone.scalar(
            select(func.coalesce(func.sum(Extraction.cost_usd), 0)).where(
                Extraction.created_at >= start_of_day()
            )
        )
    return Decimal(str(total or 0))


def over_cap(session: Session) -> bool:
    """True when a cap is set and today's spend has reached it."""
    cap = get_settings().max_daily_spend_usd
    return cap is not None and spent_today(session) >= cap
