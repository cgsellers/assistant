"""
Who may use each processor, and routing per organisation (step 4.4, DESIGN §9.3).

A processor is for every organisation or for those granted it; a new one is
for nobody. A client's documents route only among the processors it may use,
and on an exact tie a processor dedicated to it beats a shared one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import auth, repository, tenancy, users
from core.models import Job, OcrResult, ProcessorAccess
from core.scoping import Scope, set_scope
from core.status import DocStatus, JobKind
from doctypes import authoring, registry
from doctypes.authoring import AuthoringError
from doctypes.definition import Definition
from worker import run as worker

from .conftest import FakeExtractor, demo_org_id

pytestmark = pytest.mark.usefixtures("fast_hashing")

TEXT = "CAFE LUNA\nTICKET 0042\nTOTAL 9.35"


def _receipt(name: str, keywords=("ticket", "total")) -> Definition:
    return Definition.model_validate({
        "name": name,
        "matches": {"keywords": list(keywords), "min_score": 0.5},
        "fields": {"total": {"type": "money", "description": "The total."}},
        "canonical": {"direction": "out", "amount": "total"},
    })


@pytest.fixture(autouse=True)
def _cold_registry():
    registry.clear_cache()
    yield
    registry.clear_cache()


@pytest.fixture
def orgs(session):
    return {slug: tenancy.create(session, slug.title(), slug) for slug in ("acme", "globex")}


def _publish(session, definition: Definition, *, everyone=False, grant=()):
    repository.create_document_type_version(session, definition)
    authoring.set_access(
        session, definition.name, available_to_all=everyone,
        organisation_ids=[o.id for o in grant], changed_by="test",
    )


def _route(session, organisation) -> str | None:
    match = registry.route(session, TEXT, organisation.id)
    return match.definition.name if match else None


class TestRouting:
    def test_a_new_processor_is_for_nobody(self, session, orgs):
        repository.create_document_type_version(session, _receipt("till"))
        assert _route(session, orgs["acme"]) is None
        assert authoring.access(session, "till").nobody

    def test_one_for_everyone_routes_every_organisations_documents(self, session, orgs):
        _publish(session, _receipt("till"), everyone=True)
        assert _route(session, orgs["acme"]) == "till"
        assert _route(session, orgs["globex"]) == "till"

    def test_a_granted_one_routes_only_its_organisations(self, session, orgs):
        _publish(session, _receipt("acme_till"), grant=[orgs["acme"]])
        assert _route(session, orgs["acme"]) == "acme_till"
        assert _route(session, orgs["globex"]) is None

    def test_on_an_exact_tie_the_dedicated_one_wins(self, session, orgs):
        """Same keywords, same score: Acme's own receipt beats the shared one."""
        _publish(session, _receipt("a_shared"), everyone=True)  # first by name order
        _publish(session, _receipt("z_acme"), grant=[orgs["acme"]])
        assert _route(session, orgs["acme"]) == "z_acme"
        assert _route(session, orgs["globex"]) == "a_shared"

    def test_a_better_shared_match_still_beats_a_dedicated_one(self, session, orgs):
        """The tie-break is a tie-break, not a preference over evidence."""
        _publish(session, _receipt("shared", ("ticket", "total", "luna")), everyone=True)
        _publish(
            session, _receipt("acme_own", ("ticket", "total", "factura")), grant=[orgs["acme"]]
        )
        assert _route(session, orgs["acme"]) == "shared"

    def test_the_teams_tools_still_see_every_live_processor(self, session, orgs):
        """No organisation: the eval harness and trials on text route among all."""
        repository.create_document_type_version(session, _receipt("till"))
        assert registry.route(session, TEXT).definition.name == "till"


class TestChangingAccess:
    def test_grants_are_replaced_and_kept_ones_keep_their_history(self, session, orgs):
        repository.create_document_type_version(session, _receipt("till"))
        authoring.set_access(
            session, "till", available_to_all=False,
            organisation_ids=[orgs["acme"].id], changed_by="ana",
        )
        first = session.get(ProcessorAccess, ("till", orgs["acme"].id)).granted_at

        result = authoring.set_access(
            session, "till", available_to_all=False,
            organisation_ids=[orgs["acme"].id, orgs["globex"].id], changed_by="bo",
        )

        assert [o.slug for o in result.organisations] == ["acme", "globex"]
        assert session.get(ProcessorAccess, ("till", orgs["acme"].id)).granted_at == first
        assert session.get(ProcessorAccess, ("till", orgs["globex"].id)).granted_by == "bo"
        assert result.changed_by == "bo"

    def test_taking_access_away_takes_effect_at_once(self, session, orgs):
        _publish(session, _receipt("till"), grant=[orgs["acme"]])
        authoring.set_access(session, "till", available_to_all=False, organisation_ids=[],
                             changed_by="ana")
        assert _route(session, orgs["acme"]) is None

    def test_an_unknown_organisation_is_refused(self, session, orgs):
        repository.create_document_type_version(session, _receipt("till"))
        with pytest.raises(AuthoringError, match="no such organisation"):
            authoring.set_access(session, "till", available_to_all=False,
                                 organisation_ids=["nope"], changed_by="ana")

    def test_the_starter_seeds_are_for_everyone(self, session):
        from doctypes import seed

        seed.sync(session, seed.load_seeds())
        assert authoring.access(session, "invoice").available_to_all
        assert authoring.access(session, "receipt").available_to_all


class TestThroughTheWorker:
    def _process(self, session, organisation, text=TEXT):
        document, _ = repository.ingest(
            session, organisation_id=organisation.id, data=organisation.slug.encode() * 7,
            mime_type="image/png", original_filename="r.png",
        )
        session.query(Job).delete()
        document.status = DocStatus.OCR_DONE
        session.add(OcrResult(
            document_id=document.id, engine="fake", engine_version="1", params={},
            full_text=text, layout_blocks=[], mean_confidence=None,
        ))
        session.add(Job(kind=JobKind.EXTRACT, document_id=document.id))
        session.commit()
        worker.run_extract_job(
            session, worker.claim(session, me="t"), FakeExtractor({"total": "9.35"})
        )
        session.refresh(document)
        return document

    def test_each_organisation_is_routed_among_its_own_processors(self, session, orgs):
        _publish(session, _receipt("a_shared"), everyone=True)
        _publish(session, _receipt("z_acme"), grant=[orgs["acme"]])

        acme = self._process(session, orgs["acme"])
        globex = self._process(session, orgs["globex"])

        assert repository.current_extraction(session, acme.id).doc_type == "z_acme"
        assert repository.current_extraction(session, globex.id).doc_type == "a_shared"
        # The routing record shows only what that organisation could choose from.
        classified = [e for e in globex.events if e.to_status == DocStatus.CLASSIFIED][-1]
        assert set(classified.detail["scores"]) == {"a_shared"}

    def test_a_document_nothing_it_may_use_matches_is_unsupported(self, session, orgs):
        _publish(session, _receipt("acme_only"), grant=[orgs["acme"]])
        assert self._process(session, orgs["globex"]).status == DocStatus.UNSUPPORTED


class TestTrials:
    def test_a_trial_says_when_the_documents_organisation_could_not_use_it(
        self, session, orgs
    ):
        _publish(session, _receipt("acme_till"), grant=[orgs["acme"]])
        globex_doc, _ = repository.ingest(
            session, organisation_id=orgs["globex"].id, data=b"g" * 20,
            mime_type="image/png", original_filename="g.png",
        )
        session.add(OcrResult(
            document_id=globex_doc.id, engine="fake", engine_version="1", params={},
            full_text=TEXT, layout_blocks=[], mean_confidence=None,
        ))
        session.commit()
        trial = authoring.trial_run(
            session, "acme_till", 1, extractor=FakeExtractor({"total": "9.35"}),
            document_id=globex_doc.id,
        )
        assert trial.allowed is False


class TestTheApiAndPanel:
    @pytest.fixture
    def admin(self, _wired, session):
        _, key = auth.create(session, "team admin", organisation_id=demo_org_id(session),
                             role="admin")
        with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as c:
            yield c

    def test_access_is_read_and_changed_by_slug(self, admin, session, orgs):
        repository.create_document_type_version(session, _receipt("till"))
        assert admin.get("/processors/till/access").json()["nobody"] is True

        changed = admin.put(
            "/processors/till/access", json={"available_to_all": False, "organisations": ["acme"]}
        ).json()
        assert changed["organisations"] == ["acme"]
        assert changed["changed_by"] == "key:team admin"

    def test_an_unknown_slug_is_a_422(self, admin, session, orgs):
        repository.create_document_type_version(session, _receipt("till"))
        response = admin.put(
            "/processors/till/access", json={"available_to_all": False, "organisations": ["x"]}
        )
        assert response.status_code == 422

    def test_an_author_may_look_but_not_change_it(self, _wired, session, orgs):
        repository.create_document_type_version(session, _receipt("till"))
        _, key = auth.create(session, "drafting agent", organisation_id=demo_org_id(session))
        with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as c:
            assert c.get("/processors/till/access").status_code == 200
            assert c.put("/processors/till/access",
                         json={"available_to_all": True}).status_code == 403

    def test_the_panel_warns_about_a_live_processor_nobody_can_use(self, _wired, session, orgs):
        draft = authoring.create_draft(session, _receipt("till"), based_on=None, created_by="t")
        authoring.publish(session, "till", draft.version, published_by="t")
        users.create(session, "ana@ledgerline.test", "correct horse battery",
                     organisation_id=demo_org_id(session), is_staff=True, role="admin")
        with TestClient(app, base_url="https://testserver") as browser:
            browser.post("/panel/login", data={"email": "ana@ledgerline.test",
                                               "password": "correct horse battery"})
            listing = browser.get("/panel").text
            page = browser.get("/panel/processors/till").text
            token = page.split('"X-CSRF-Token": "')[1].split('"')[0]
            browser.post(
                "/panel/processors/till/access",
                data={"who": "some", "organisation": [orgs["acme"].id]},
                headers={"X-CSRF-Token": token, "HX-Request": "true"},
            )
        assert "nobody" in listing
        assert "It is live, but no organisation" in page
        assert [o.slug for o in authoring.access(session, "till").organisations] == ["acme"]


class TestPerClientViews:
    def _spend(self, session, organisation, amount):
        document, _ = repository.ingest(
            session, organisation_id=organisation.id, data=organisation.slug.encode() * 5,
            mime_type="image/png", original_filename="x.png",
        )
        extraction = repository.record_extraction(
            session, document_id=document.id, doc_type="till", schema_version=1,
            model_used="m", raw_json={}, cost_usd=Decimal("0.01"),
        )
        repository.record_transaction(
            session, extraction=extraction,
            values={"source_type": "till", "direction": "out", "amount": Decimal(amount),
                    "currency": "EUR"},
        )

    def test_staff_see_spend_split_by_organisation_and_a_client_only_its_own(
        self, session, session_factory, orgs
    ):
        from core.costs import cost_report

        self._spend(session, orgs["acme"], "10")
        self._spend(session, orgs["globex"], "20")

        everyone = {s.key: s.calls for s in cost_report(session).by_organisation}
        assert everyone == {"Acme": 1, "Globex": 1}
        with session_factory() as acme:
            set_scope(acme, Scope.of(orgs["acme"].id, "acme"))
            assert [s.key for s in cost_report(acme).by_organisation] == ["Acme"]

    def test_the_analyst_answers_from_one_organisations_ledger(
        self, session, session_factory, orgs
    ):
        from analyst import tools

        self._spend(session, orgs["acme"], "10")
        self._spend(session, orgs["globex"], "20")
        with session_factory() as acme:
            set_scope(acme, Scope.of(orgs["acme"].id, "acme"))
            totals = tools.total_spend(acme).as_dict()["totals"]
        assert totals == [{"currency": "EUR", "total": "10.00", "transactions": 1}]
