"""
Routing, and the bookkeeping underneath it: which version of a type is live,
and which extraction counts as a document's result.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core import repository
from core.models import DocumentTypeDefinition, Extraction
from doctypes import registry
from doctypes.definition import Definition, FieldSpec, FieldType, Matchers, Profile

from .conftest import TINY_PNG

# A document that is unambiguously an invoice and only weakly a receipt.
INVOICE_TEXT = """
FACTURA  N. 2024/0117
ACME SUMINISTROS S.L.  NIF B12345678
BASE IMPONIBLE   100,00
IVA 21%           21,00
TOTAL            121,00
"""


def _definition(
    name: str,
    *,
    keywords: list[str] | None = None,
    require_all: list[str] | None = None,
    patterns: list[str] | None = None,
    min_score: float = 0.35,
    profiles: dict[str, Profile] | None = None,
) -> Definition:
    return Definition(
        name=name,
        matches=Matchers(
            keywords=keywords or [],
            require_all=require_all or [],
            patterns=patterns or [],
            min_score=min_score,
        ),
        fields={"total": FieldSpec(type=FieldType.MONEY, description="The grand total.")},
        profiles=profiles or {},
    )


def _invoice(**kwargs) -> Definition:
    """Scores 1.0 on INVOICE_TEXT: every keyword hits."""
    return _definition(
        "invoice",
        keywords=["factura", "base imponible", "iva"],
        **kwargs,
    )


def _receipt() -> Definition:
    """Scores 0.5 on INVOICE_TEXT -- above its own floor, below the invoice."""
    return _definition(
        "receipt",
        keywords=["total", "iva", "ticket", "gracias por su compra"],
    )


def _register(session, definition: Definition, **kwargs) -> DocumentTypeDefinition:
    return repository.create_document_type_version(session, definition, **kwargs)


def _document(session):
    document, _ = repository.ingest(
        session, data=TINY_PNG, mime_type="image/png", original_filename="x.png"
    )
    return document


@pytest.fixture(autouse=True)
def _cold_cache():
    """
    The parsed-definition map is module state and outlives a test database,
    where row ids from a previous tmp_path could otherwise still be present.
    """
    registry.clear_cache()
    yield
    registry.clear_cache()


class TestActiveDefinitions:
    def test_returns_published_active_types(self, session):
        _register(session, _invoice())
        _register(session, _receipt())

        names = [d.name for d in registry.active_definitions(session)]
        assert names == ["invoice", "receipt"]  # name order, for stable ties

    def test_excludes_inactive_versions(self, session):
        _register(session, _invoice())
        _register(session, _invoice())  # v2 takes the flag off v1

        definitions = registry.active_definitions(session)
        assert [d.version for d in definitions] == [2]

    def test_excludes_unpublished_drafts(self, session):
        _register(session, _receipt(), publish=False, activate=False)

        assert registry.active_definitions(session) == []

    def test_parses_the_json_column_into_a_definition(self, session):
        _register(session, _invoice())

        definition = registry.active_definitions(session)[0]
        assert isinstance(definition, Definition)
        assert definition.fields["total"].type is FieldType.MONEY


class TestRouting:
    def test_picks_the_highest_scorer(self, session):
        _register(session, _invoice())
        _register(session, _receipt())

        result = registry.route(session, INVOICE_TEXT)

        assert result is not None
        assert result.definition.name == "invoice"
        assert result.score == pytest.approx(1.0)

    def test_returns_none_when_nothing_matches(self, session):
        """The caller's cue to escalate to a model classifier, not an error."""
        _register(session, _invoice())
        _register(session, _receipt())

        assert registry.route(session, "BOARDING PASS  GATE 14  SEAT 22C") is None

    def test_returns_none_with_no_types_registered(self, session):
        assert registry.route(session, INVOICE_TEXT) is None

    def test_a_type_below_its_own_min_score_is_not_a_match(self, session):
        """0.5 of the keywords hit, but this type's author demanded 0.9."""
        _register(session, _definition("strict", keywords=["factura", "albaran"], min_score=0.9))

        assert registry.route(session, INVOICE_TEXT) is None

    def test_require_all_gates_a_type_out_entirely(self, session):
        _register(session, _invoice())
        _register(
            session,
            _definition(
                "credit_note",
                keywords=["factura", "iva"],
                require_all=["factura rectificativa"],
            ),
        )

        result = registry.route(session, INVOICE_TEXT)
        assert result is not None
        assert result.definition.name == "invoice"

    def test_ties_break_by_name_for_reproducibility(self, session):
        _register(session, _definition("aaa_type", keywords=["factura"]))
        _register(session, _definition("zzz_type", keywords=["factura"]))

        result = registry.route(session, INVOICE_TEXT)
        assert result is not None
        assert result.definition.name == "aaa_type"

    def test_inactive_type_does_not_win_even_scoring_higher(self, session):
        """A retired type outscores the live one; routing must still ignore it."""
        _register(session, _receipt())
        session.add(
            DocumentTypeDefinition(
                name="invoice",
                version=1,
                definition=_invoice().model_dump(mode="json"),
                is_active=False,
                is_published=True,
            )
        )
        session.commit()

        result = registry.route(session, INVOICE_TEXT)
        assert result is not None
        assert result.definition.name == "receipt"

    def test_unpublished_draft_does_not_win(self, session):
        _register(session, _receipt())
        # A draft of a better-matching type, mid-authoring.
        row = DocumentTypeDefinition(
            name="invoice",
            version=1,
            definition=_invoice().model_dump(mode="json"),
            is_active=True,
            is_published=False,
        )
        session.add(row)
        session.commit()

        result = registry.route(session, INVOICE_TEXT)
        assert result is not None
        assert result.definition.name == "receipt"


class TestMalformedDefinitions:
    def _add_broken_row(self, session) -> None:
        session.add(
            DocumentTypeDefinition(
                name="broken",
                version=1,
                # No matchers, no fields: valid JSON, invalid Definition.
                definition={"name": "broken", "version": 1},
                is_active=True,
                is_published=True,
            )
        )
        session.commit()

    def test_one_bad_row_does_not_stop_the_others_routing(self, session):
        self._add_broken_row(session)
        _register(session, _invoice())

        result = registry.route(session, INVOICE_TEXT)
        assert result is not None
        assert result.definition.name == "invoice"

    def test_bad_row_is_skipped_not_raised(self, session):
        self._add_broken_row(session)
        _register(session, _invoice())

        assert [d.name for d in registry.active_definitions(session)] == ["invoice"]

    def test_bad_row_is_logged_with_its_identity(self, session, caplog):
        self._add_broken_row(session)

        with caplog.at_level("ERROR", logger="doctypes.registry"):
            registry.active_definitions(session)

        assert "broken" in caplog.text

    def test_only_bad_rows_leave_routing_empty(self, session):
        self._add_broken_row(session)
        assert registry.route(session, INVOICE_TEXT) is None


class TestProfileResolution:
    def _acme(self) -> Definition:
        return _invoice(
            profiles={
                "acme": Profile(
                    tax_ids=["B12345678"],
                    descriptions={"total": "Labelled TOTAL, bottom right of the box."},
                ),
                "other": Profile(tax_ids=["B99999999"]),
            }
        )

    def test_resolves_the_issuer_profile_on_the_winner(self, session):
        _register(session, self._acme())

        result = registry.route(session, INVOICE_TEXT)

        assert result is not None
        assert result.profile_key == "acme"
        assert result.profile is not None
        assert result.profile.descriptions["total"].startswith("Labelled TOTAL")

    def test_unknown_issuer_routes_without_a_profile(self, session):
        _register(session, self._acme())

        result = registry.route(session, INVOICE_TEXT.replace("B12345678", "B55555555"))

        assert result is not None
        assert result.definition.name == "invoice"
        assert result.profile_key is None
        assert result.profile is None

    def test_a_losing_types_profile_is_not_returned(self, session):
        _register(session, _invoice())
        _register(
            session,
            _definition(
                "receipt",
                keywords=["total", "iva", "ticket", "gracias por su compra"],
                profiles={"acme": Profile(tax_ids=["B12345678"])},
            ),
        )

        result = registry.route(session, INVOICE_TEXT)
        assert result is not None
        assert result.definition.name == "invoice"
        assert result.profile_key is None


class TestDefinitionCache:
    def test_a_new_active_version_is_routed_to_immediately(self, session):
        """The staleness bug the cache key exists to prevent."""
        _register(session, _definition("invoice", keywords=["factura"]))
        assert registry.route(session, INVOICE_TEXT) is not None

        _register(session, _definition("invoice", keywords=["albaran de entrega"]))

        assert registry.route(session, INVOICE_TEXT) is None

    def test_an_edited_row_is_reparsed(self, session):
        row = _register(session, _invoice())
        assert registry.route(session, INVOICE_TEXT) is not None

        row.definition = _definition("invoice", keywords=["albaran"]).model_dump(mode="json")
        session.commit()

        assert registry.route(session, INVOICE_TEXT) is None


class TestDocumentTypeVersions:
    def test_first_version_is_one_and_active(self, session):
        row = _register(session, _invoice())

        assert row.version == 1
        assert row.is_active is True
        assert row.is_published is True

    def test_a_new_version_deactivates_the_previous_one(self, session):
        first = _register(session, _invoice())
        second = _register(session, _invoice())

        session.refresh(first)
        assert second.version == 2
        assert second.is_active is True
        assert first.is_active is False

    def test_only_ever_one_active_version_per_name(self, session):
        for _ in range(4):
            _register(session, _invoice())

        active = session.query(DocumentTypeDefinition).filter_by(name="invoice", is_active=True)
        assert active.count() == 1
        assert session.query(DocumentTypeDefinition).count() == 4

    def test_versions_are_numbered_per_name(self, session):
        _register(session, _invoice())
        _register(session, _invoice())
        receipt = _register(session, _receipt())

        assert receipt.version == 1

    def test_the_stored_json_carries_the_assigned_version(self, session):
        """Otherwise extractions cite a schema_version the definition denies."""
        _register(session, _invoice())
        second = _register(session, _invoice())

        assert second.definition["version"] == 2
        assert registry.active_definitions(session)[0].version == 2

    def test_a_draft_leaves_the_active_version_alone(self, session):
        live = _register(session, _invoice())
        draft = _register(session, _invoice(), publish=False, activate=False)

        session.refresh(live)
        assert draft.version == 2
        assert draft.is_active is False
        assert live.is_active is True

    def test_activating_an_unpublished_version_is_refused(self, session):
        with pytest.raises(ValueError, match="unpublished"):
            _register(session, _invoice(), publish=False, activate=True)

    def test_get_active_document_type_returns_the_live_row(self, session):
        _register(session, _invoice())
        second = _register(session, _invoice())

        found = repository.get_active_document_type(session, "invoice")
        assert found is not None
        assert found.id == second.id

    def test_get_active_document_type_is_none_when_unregistered(self, session):
        assert repository.get_active_document_type(session, "nothing_here") is None


class TestRecordExtraction:
    def _record(self, session, document, **kwargs):
        defaults = {
            "document_id": document.id,
            "doc_type": "invoice",
            "schema_version": 1,
            "model_used": "claude-test",
        }
        return repository.record_extraction(session, **(defaults | kwargs))

    def test_first_extraction_is_current(self, session):
        document = _document(session)
        extraction = self._record(session, document, raw_json={"total": "121.00"})

        assert extraction.is_current is True
        assert extraction.attempt == 1
        assert extraction.is_trial is False

    def test_a_later_extraction_supersedes_the_earlier_one(self, session):
        document = _document(session)
        first = self._record(session, document)
        second = self._record(session, document)

        session.refresh(first)
        assert first.is_current is False
        assert second.is_current is True
        assert second.attempt == 2

    def test_exactly_one_current_row_across_several_attempts(self, session):
        document = _document(session)
        for _ in range(3):
            self._record(session, document)

        current = session.query(Extraction).filter_by(document_id=document.id, is_current=True)
        assert current.count() == 1
        assert session.query(Extraction).count() == 3

    def test_a_trial_is_never_current(self, session):
        document = _document(session)
        trial = self._record(session, document, is_trial=True)

        assert trial.is_trial is True
        assert trial.is_current is False

    def test_a_trial_does_not_displace_the_real_result(self, session):
        document = _document(session)
        real = self._record(session, document, raw_json={"total": "121.00"})
        self._record(session, document, is_trial=True, raw_json={"total": "999.00"})

        session.refresh(real)
        assert real.is_current is True

    def test_trials_are_numbered_apart_from_real_attempts(self, session):
        document = _document(session)
        self._record(session, document, is_trial=True)
        self._record(session, document, is_trial=True)
        real = self._record(session, document)

        assert real.attempt == 1

    def test_current_is_scoped_to_one_document(self, session):
        first = _document(session)
        second, _ = repository.ingest(
            session, data=TINY_PNG + b"other", mime_type="image/png", original_filename="b.png"
        )
        a = self._record(session, first)
        self._record(session, second)

        session.refresh(a)
        assert a.is_current is True

    def test_records_the_profile_and_cost(self, session):
        document = _document(session)
        extraction = self._record(
            session,
            document,
            profile="acme",
            cost_usd=Decimal("0.004821"),
            input_tokens=1200,
            output_tokens=180,
            latency_ms=2300,
        )

        session.refresh(extraction)
        assert extraction.profile == "acme"
        assert extraction.cost_usd == Decimal("0.004821")
        assert isinstance(extraction.cost_usd, Decimal)

    def test_a_float_cost_is_refused(self, session):
        """Money is Decimal; these rows get summed into a cost report."""
        document = _document(session)
        with pytest.raises(TypeError, match="Decimal"):
            self._record(session, document, cost_usd=0.004821)

    def test_route_result_feeds_an_extraction_row(self, session):
        """The handoff the two halves of phase 2 exist to make."""
        document = _document(session)
        _register(
            session,
            _invoice(profiles={"acme": Profile(tax_ids=["B12345678"])}),
        )
        result = registry.route(session, INVOICE_TEXT)
        assert result is not None

        extraction = repository.record_extraction(
            session,
            document_id=document.id,
            doc_type=result.definition.name,
            schema_version=result.definition.version,
            profile=result.profile_key,
            model_used="claude-test",
            raw_json={"total": "121.00"},
        )

        assert extraction.doc_type == "invoice"
        assert extraction.schema_version == 1
        assert extraction.profile == "acme"
