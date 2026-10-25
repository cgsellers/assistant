"""
The authoring rules: what every way of editing a processor goes through.

Mostly the lifecycle (draft, publish, retire) and the lost-update guard,
because those are where a mistake silently costs somebody their edit or
sends documents to a version nobody meant to be live.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core import repository
from core.models import DocumentTypeDefinition, Extraction, OcrResult, Transaction
from doctypes import authoring, registry
from doctypes.authoring import AuthoringError, Conflict, NotFound

from .conftest import TINY_PNG, FakeExtractor, demo_org_id

TEXT = "CAFE LUNA\nTICKET 0042\nTOTAL 9.35"


def _spec(name: str = "till", **overrides) -> dict:
    spec = {
        "name": name,
        "matches": {"keywords": ["ticket", "total"], "min_score": 0.5},
        "fields": {
            "total": {"type": "money", "description": "The total."},
            "vendor": {"type": "string", "description": "The vendor."},
        },
        "rules": [{"type": "required", "field": "total"}],
    }
    spec.update(overrides)
    return spec


@pytest.fixture(autouse=True)
def _cold_registry():
    registry.clear_cache()
    yield
    registry.clear_cache()


def _published(session, name="till", **overrides) -> DocumentTypeDefinition:
    draft = authoring.create_draft(session, _spec(name, **overrides), based_on=None, created_by="t")
    return authoring.publish(session, name, draft.version, published_by="t")


class TestDrafting:
    def test_a_new_processor_starts_as_a_draft_nothing_routes_to(self, session):
        draft = authoring.create_draft(session, _spec(), based_on=None, created_by="ana")

        assert (draft.version, draft.is_published, draft.is_active) == (1, False, False)
        assert draft.created_by == "ana"
        assert registry.route(session, TEXT) is None

    def test_an_invalid_definition_lists_every_problem_with_its_path(self, session):
        spec = _spec(fields={"Total": {"type": "money", "description": ""}})
        with pytest.raises(AuthoringError) as caught:
            authoring.create_draft(session, spec, based_on=None, created_by="t")
        assert caught.value.details
        assert all(":" in d for d in caught.value.details)

    def test_an_existing_processor_must_be_drafted_from_a_version(self, session):
        _published(session)
        with pytest.raises(Conflict, match="start the draft from v1") as caught:
            authoring.create_draft(session, _spec(), based_on=None, created_by="t")
        assert caught.value.latest == 1

    def test_a_draft_cannot_be_based_on_a_draft(self, session):
        first = authoring.create_draft(session, _spec(), based_on=None, created_by="t")
        with pytest.raises(AuthoringError, match="is a draft"):
            authoring.create_draft(session, _spec(), based_on=first.version, created_by="t")

    def test_a_draft_is_edited_in_place(self, session):
        draft = authoring.create_draft(session, _spec(), based_on=None, created_by="t")
        authoring.update_draft(session, "till", draft.version, _spec(display_name="Till roll"))

        assert session.query(DocumentTypeDefinition).count() == 1
        session.refresh(draft)
        assert draft.display_name == "Till roll"
        assert draft.definition["version"] == draft.version

    def test_a_published_version_is_never_edited(self, session):
        live = _published(session)
        with pytest.raises(AuthoringError, match="never change"):
            authoring.update_draft(session, "till", live.version, _spec(display_name="X"))

    def test_a_draft_cannot_be_renamed(self, session):
        draft = authoring.create_draft(session, _spec(), based_on=None, created_by="t")
        with pytest.raises(AuthoringError, match="renamed"):
            authoring.update_draft(session, "till", draft.version, _spec("other"))


class TestPublishing:
    def test_publishing_makes_it_the_live_version(self, session):
        live = _published(session)

        assert (live.is_published, live.is_active) == (True, True)
        assert registry.route(session, TEXT).definition.name == "till"

    def test_a_new_version_replaces_the_old_one_in_one_step(self, session):
        _published(session)
        draft = authoring.create_draft(
            session, _spec(display_name="v2"), based_on=1, created_by="t"
        )
        authoring.publish(session, "till", draft.version, published_by="t")

        active = session.query(DocumentTypeDefinition).filter_by(is_active=True).all()
        assert [r.version for r in active] == [2]

    def test_publishing_twice_is_refused(self, session):
        live = _published(session)
        with pytest.raises(AuthoringError, match="already published"):
            authoring.publish(session, "till", live.version, published_by="t")

    def test_a_transform_that_fails_its_examples_is_not_published(self, session):
        spec = _spec(transform={
            "code": "def transform(data):\n    return data\n",
            "examples": [{"input": {"total": "1.00"}, "expected": {"total": "2.00"}}],
        })
        draft = authoring.create_draft(session, spec, based_on=None, created_by="t")

        with pytest.raises(AuthoringError, match="fails its examples") as caught:
            authoring.publish(session, "till", draft.version, published_by="t")
        assert caught.value.details == ["example 1: total should be '2.00', got '1.00'"]
        session.refresh(draft)
        assert draft.is_published is False


class TestTheLostUpdateGuard:
    """Two people, one processor, and nobody's work quietly disappearing."""

    def test_the_second_of_two_drafts_from_the_same_version_conflicts(self, session):
        _published(session)
        mine = authoring.create_draft(
            session, _spec(display_name="mine"), based_on=1, created_by="a"
        )
        theirs = authoring.create_draft(
            session, _spec(display_name="theirs"), based_on=1, created_by="b"
        )

        authoring.publish(session, "till", theirs.version, published_by="b")
        with pytest.raises(Conflict, match="merge its changes") as caught:
            authoring.publish(session, "till", mine.version, published_by="a")

        assert caught.value.latest == theirs.version
        live = repository.get_active_document_type(session, "till")
        assert live.display_name == "theirs"

    def test_two_brand_new_processors_with_one_name_conflict_too(self, session):
        first = authoring.create_draft(session, _spec(), based_on=None, created_by="a")
        second = authoring.create_draft(session, _spec(), based_on=None, created_by="b")

        authoring.publish(session, "till", first.version, published_by="a")
        with pytest.raises(Conflict):
            authoring.publish(session, "till", second.version, published_by="b")

    def test_a_draft_from_the_latest_version_publishes(self, session):
        _published(session)
        draft = authoring.create_draft(session, _spec(), based_on=1, created_by="a")
        assert authoring.publish(session, "till", draft.version, published_by="a").is_active


class TestRetiring:
    def test_a_retired_processor_stops_routing_and_keeps_its_history(self, session):
        _published(session)
        authoring.retire(session, "till", retired_by="t")

        assert registry.route(session, TEXT) is None
        assert len(authoring.history(session, "till")) == 1
        assert authoring.list_processors(session)[0].status == "retired"

    def test_publishing_a_draft_brings_it_back(self, session):
        _published(session)
        authoring.retire(session, "till", retired_by="t")
        draft = authoring.create_draft(session, _spec(), based_on=1, created_by="t")
        authoring.publish(session, "till", draft.version, published_by="t")

        assert registry.route(session, TEXT).definition.version == 2

    def test_retiring_what_is_not_active_is_refused(self, session):
        authoring.create_draft(session, _spec(), based_on=None, created_by="t")
        with pytest.raises(AuthoringError, match="not active"):
            authoring.retire(session, "till", retired_by="t")

    def test_an_unknown_processor_is_not_found(self, session):
        with pytest.raises(NotFound):
            authoring.retire(session, "nothing", retired_by="t")


class TestListing:
    def test_each_processor_is_summarised_once(self, session):
        _published(session)
        authoring.create_draft(session, _spec(), based_on=1, created_by="t")
        authoring.create_draft(session, _spec("fresh"), based_on=None, created_by="t")

        summaries = {s.name: s for s in authoring.list_processors(session)}
        assert summaries["till"].status == "active"
        assert summaries["till"].drafts == [2]
        assert summaries["fresh"].status == "draft"


class TestTrialRuns:
    DATA = {"total": "9.35", "vendor": "CAFE LUNA"}

    def _document(self, session):
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG, mime_type="image/png", original_filename="a.png"
        )
        session.add(OcrResult(
            document_id=document.id, engine="fake", engine_version="1", params={},
            full_text=TEXT, layout_blocks=[], mean_confidence=None,
        ))
        session.commit()
        return document

    def test_a_draft_can_be_tried_on_text(self, session):
        draft = authoring.create_draft(session, _spec(), based_on=None, created_by="t")

        trial = authoring.trial_run(
            session, "till", draft.version, extractor=FakeExtractor(self.DATA), text=TEXT
        )

        assert trial.routed_to == "till"  # as if it were live
        assert trial.data == {"total": "9.35", "vendor": "CAFE LUNA"}
        assert trial.errors == []
        assert trial.cost_usd == Decimal("0.001205")
        assert trial.extraction_id is None  # loose text: nothing to attach it to

    def test_it_shows_when_another_processor_would_win(self, session):
        _published(session, "receipt", matches={"keywords": ["ticket", "total", "luna"]})
        draft = authoring.create_draft(session, _spec(), based_on=None, created_by="t")

        trial = authoring.trial_run(
            session, "till", draft.version, extractor=FakeExtractor(self.DATA), text=TEXT
        )
        assert trial.routed_to == "receipt"
        assert trial.scores["receipt"]["hits"] == 3
        assert trial.scores["till"]["hits"] == 2

    def test_rule_failures_are_reported_not_raised(self, session):
        draft = authoring.create_draft(session, _spec(), based_on=None, created_by="t")
        trial = authoring.trial_run(
            session, "till", draft.version, extractor=FakeExtractor({}), text=TEXT
        )
        assert any("total" in e for e in trial.errors)

    def test_a_trial_on_a_stored_document_is_recorded_but_never_counts(self, session):
        _published(session)
        document = self._document(session)

        trial = authoring.trial_run(
            session, "till", 1, extractor=FakeExtractor(self.DATA), document_id=document.id
        )

        extraction = session.get(Extraction, trial.extraction_id)
        assert extraction.is_trial is True
        assert extraction.is_current is False
        assert extraction.cost_usd == Decimal("0.001205")
        assert session.query(Transaction).count() == 0

    def test_it_needs_something_to_run_on(self, session):
        draft = authoring.create_draft(session, _spec(), based_on=None, created_by="t")
        with pytest.raises(AuthoringError, match="stored document or some text"):
            authoring.trial_run(session, "till", draft.version, extractor=FakeExtractor())

    def test_a_document_without_ocr_is_explained(self, session):
        draft = authoring.create_draft(session, _spec(), based_on=None, created_by="t")
        document, _ = repository.ingest(
            session,
            organisation_id=demo_org_id(session),
            data=TINY_PNG, mime_type="image/png", original_filename="a.png"
        )
        with pytest.raises(AuthoringError, match="not been through OCR"):
            authoring.trial_run(
                session, "till", draft.version, extractor=FakeExtractor(),
                document_id=document.id,
            )
