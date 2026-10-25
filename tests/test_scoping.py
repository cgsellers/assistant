"""
The scoping hook (core/scoping.py): whose client data a session sees.

Written as probes of query *shapes*, because that is how it failed: the
hook's first version decided whether a statement touched client data by
looking only at the entities it selected, so `select(func.count())
.select_from(Document)` -- which selects none -- skipped the filter and the
unscoped check, and counted every client's documents. Three tests here fail
against that version. Each is a shape somebody could write next year without
thinking about tenancy. Under TEST_DATABASE_URL they all run on Postgres too.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import aliased, joinedload, selectinload

from core import repository, tenancy
from core.models import Document, DocumentEvent, Extraction, Organisation, Transaction
from core.scoping import Scope, Unscoped, get_scope, set_scope


@pytest.fixture
def world(session):
    """Two organisations, each with one document, extraction and ledger row."""
    ids = {}
    for slug in ("acme", "globex"):
        org = tenancy.create(session, slug.title(), slug)
        document, _ = repository.ingest(
            session, organisation_id=org.id, data=slug.encode() * 8,
            mime_type="image/png", original_filename=f"{slug}.png",
        )
        extraction = repository.record_extraction(
            session, document_id=document.id, doc_type="receipt", schema_version=1,
            model_used="m", raw_json={"who": slug}, cost_usd=Decimal("0.01"),
        )
        repository.record_transaction(
            session, extraction=extraction,
            values={"source_type": "receipt", "direction": "out", "amount": Decimal("1")},
        )
        ids[slug] = {"org": org.id, "document": document.id}
    return ids


@pytest.fixture
def as_org(session_factory, world):
    """A fresh session scoped to one organisation, as a client's request would be."""
    opened = []

    def _open(slug: str):
        s = session_factory()
        set_scope(s, Scope.of(world[slug]["org"], f"test:{slug}"))
        opened.append(s)
        return s

    yield _open
    for s in opened:
        s.close()


def _names(rows):
    return sorted(rows)


class TestShapes:
    def test_selecting_the_entity(self, as_org):
        s = as_org("acme")
        assert _names(d.original_filename for d in s.scalars(select(Document))) == ["acme.png"]

    def test_a_count_with_nothing_selected_from_the_table(self, as_org):
        """The shape that leaked: count(*) FROM documents."""
        s = as_org("acme")
        assert s.scalar(select(func.count()).select_from(Document)) == 1

    def test_a_table_that_is_only_joined_through(self, as_org):
        """The other shape that leaked: documents only in a JOIN."""
        s = as_org("acme")
        rows = s.scalars(
            select(Organisation.name).join(Document, Document.organisation_id == Organisation.id)
        ).all()
        assert rows == ["Acme"]

    def test_a_correlated_scalar_subquery(self, as_org):
        s = as_org("acme")
        spent = (
            select(func.sum(Extraction.cost_usd))
            .where(Extraction.document_id == Document.id)
            .correlate(Document)
            .scalar_subquery()
        )
        assert s.execute(select(Document.original_filename, spent)).all() == [
            ("acme.png", Decimal("0.010000"))
        ]

    def test_an_uncorrelated_subquery_in_a_where(self, as_org):
        s = as_org("acme")
        rows = s.scalars(
            select(Extraction.raw_json).where(Extraction.document_id.in_(select(Document.id)))
        ).all()
        assert rows == [{"who": "acme"}]

    def test_an_outer_join_keeps_documents_without_a_match(self, as_org, world, session):
        """The NULL allowance: no extraction is not the same as another org's."""
        session.add(DocumentEvent(
            document_id=world["acme"]["document"], from_status=None, to_status="x",
        ))
        bare, _ = repository.ingest(
            session, organisation_id=world["acme"]["org"], data=b"bare" * 9,
            mime_type="image/png", original_filename="bare.png",
        )
        session.commit()
        s = as_org("acme")
        rows = s.execute(
            select(Document.original_filename, Extraction.id)
            .outerjoin(Extraction, Extraction.document_id == Document.id)
        ).all()
        assert sorted(name for name, _ in rows) == ["acme.png", "bare.png"]

    def test_an_aliased_table(self, as_org):
        s = as_org("acme")
        other = aliased(Document)
        assert s.scalar(select(func.count(other.id))) == 1

    def test_get_by_primary_key(self, as_org, world):
        s = as_org("acme")
        assert s.get(Document, world["globex"]["document"]) is None
        assert s.get(Document, world["acme"]["document"]) is not None

    def test_relationships_lazy_and_eager(self, as_org, world):
        s = as_org("acme")
        document = s.get(Document, world["acme"]["document"])
        assert {e.organisation_id for e in document.events} == {world["acme"]["org"]}
        for option in (joinedload(Document.events), selectinload(Document.events)):
            loaded = s.scalars(select(Document).options(option)).unique().all()
            assert [d.original_filename for d in loaded] == ["acme.png"]

    def test_bulk_update_and_delete_touch_only_their_own(self, as_org, session, world):
        s = as_org("acme")
        assert s.execute(update(Transaction).values(category="mine")).rowcount == 1
        assert s.execute(delete(DocumentEvent)).rowcount == 1
        s.commit()
        session.expire_all()
        categories = dict(session.execute(
            select(Transaction.organisation_id, Transaction.category)
        ).all())
        assert categories[world["globex"]["org"]] is None
        assert session.scalar(
            select(func.count(DocumentEvent.id))
            .where(DocumentEvent.organisation_id == world["globex"]["org"])
        ) == 1

    def test_no_cached_statement_carries_one_organisations_id_to_another(self, as_org):
        """The same statement, run for two organisations in turn, twice over."""
        statement = select(Document.original_filename)
        for _ in range(2):
            assert as_org("acme").scalars(statement).all() == ["acme.png"]
            assert as_org("globex").scalars(statement).all() == ["globex.png"]


class TestFailingClosed:
    def test_an_unscoped_session_cannot_read_client_data(self, session_factory, world):
        with session_factory() as bare, pytest.raises(Unscoped, match="no scope"):
            bare.scalars(select(Document)).all()

    def test_nor_through_a_subquery(self, session_factory, world):
        with session_factory() as bare, pytest.raises(Unscoped):
            bare.scalars(select(Organisation.name).where(
                Organisation.id.in_(select(Document.organisation_id))
            )).all()

    def test_tables_that_are_not_client_data_need_no_scope(self, session_factory, world):
        with session_factory() as bare:
            assert len(bare.scalars(select(Organisation)).all()) == 2
            assert bare.execute(text("SELECT 1")).scalar() == 1

    def test_everyone_sees_everything(self, session_factory, world):
        with session_factory() as s:
            set_scope(s, Scope.everyone("staff"))
            assert s.scalar(select(func.count()).select_from(Document)) == 2

    def test_a_scope_is_set_once(self, session_factory, world):
        """Changing it would leave the old scope's objects in the identity map."""
        with session_factory() as s:
            set_scope(s, Scope.of(world["acme"]["org"], "a"))
            set_scope(s, Scope.of(world["acme"]["org"], "a"))  # the same is fine
            with pytest.raises(RuntimeError, match="already scoped"):
                set_scope(s, Scope.everyone("b"))
            assert get_scope(s).organisation_id == world["acme"]["org"]

    def test_an_organisation_scope_needs_an_organisation(self):
        with pytest.raises(ValueError):
            Scope.of("", "nobody")
