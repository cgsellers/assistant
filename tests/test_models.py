"""The UTCDateTime type decorator, which exists to fix a real bug (DESIGN 5.9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from core import repository
from core.models import Job, utcnow

from .conftest import TINY_PNG


@pytest.fixture
def document(session):
    """jobs.document_id is NOT NULL and a foreign key, so a job needs a parent."""
    doc, _ = repository.ingest(
        session, data=TINY_PNG, mime_type="image/png", original_filename="r.png"
    )
    session.query(Job).delete()  # ingest queues one; these tests add their own
    session.commit()
    return doc


class TestUtcnow:
    def test_returns_an_aware_datetime(self):
        """A naive timestamp in a finance system is a bug waiting to happen."""
        assert utcnow().tzinfo is not None

    def test_is_in_utc(self):
        assert utcnow().utcoffset() == timedelta(0)


class TestUTCDateTimeRoundTrip:
    def test_values_come_back_timezone_aware(self, session, document):
        """
        SQLite has no datetime type and returns naive strings. Without the
        decorator, run_after came back naive and comparing it to an aware now()
        raised TypeError.
        """
        session.add(Job(kind="ocr", document_id=document.id))
        session.commit()

        job = session.scalars(select(Job)).one()
        assert job.run_after.tzinfo is not None
        assert job.created_at.tzinfo is not None

    def test_the_comparison_that_used_to_crash(self, session, document):
        session.add(Job(kind="ocr", document_id=document.id))
        session.commit()

        job = session.scalars(select(Job)).one()
        assert job.run_after <= utcnow()  # TypeError before the fix

    def test_preserves_the_instant(self, session, document):
        when = datetime(2026, 5, 14, 13, 42, 0, tzinfo=UTC)
        session.add(Job(kind="ocr", document_id=document.id, run_after=when))
        session.commit()
        session.expire_all()

        assert session.scalars(select(Job)).one().run_after == when

    def test_converts_a_non_utc_offset_to_utc(self, session, document):
        """Madrid summer time is +02:00; the same instant must come back as UTC."""
        madrid = timezone(timedelta(hours=2))
        when = datetime(2026, 5, 14, 15, 42, 0, tzinfo=madrid)
        session.add(Job(kind="ocr", document_id=document.id, run_after=when))
        session.commit()
        session.expire_all()

        stored = session.scalars(select(Job)).one().run_after
        assert stored == when                       # same instant
        assert stored.utcoffset() == timedelta(0)   # expressed as UTC
        assert stored.hour == 13                    # 15:42 +02:00 -> 13:42Z

    def test_treats_a_naive_value_as_utc(self, session, document):
        naive = datetime(2026, 5, 14, 13, 42, 0)
        session.add(Job(kind="ocr", document_id=document.id, run_after=naive))
        session.commit()
        session.expire_all()

        stored = session.scalars(select(Job)).one().run_after
        assert stored == naive.replace(tzinfo=UTC)

    def test_none_stays_none(self, session, document):
        session.add(Job(kind="ocr", document_id=document.id))
        session.commit()
        assert session.scalars(select(Job)).one().locked_at is None


class TestBindParameters:
    def test_no_offset_suffix_reaches_sqlite(self, session, db_engine):
        """
        The subtler half of the bug: an aware value bound directly made SQLite
        compare '...074008' against '...039837+00:00' as strings.
        """
        captured: list = []

        @event.listens_for(db_engine, "before_cursor_execute")
        def capture(conn, cursor, statement, parameters, context, executemany):
            if "run_after" in statement:
                captured.extend(p for p in parameters if isinstance(p, str))

        session.scalars(select(Job).where(Job.run_after <= utcnow())).all()

        assert captured, "expected a bound timestamp"
        assert all("+00:00" not in p for p in captured)
