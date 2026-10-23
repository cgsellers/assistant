"""
/processors over HTTP: the translation layer, not the rules.

doctypes.authoring is tested on its own; these check that requests reach it,
that its errors come back as the right status codes with their details, and
that YAML and JSON are interchangeable ways of sending a processor.
"""

from __future__ import annotations

import pytest
import yaml

from api.main import app
from api.routes import processors
from core.models import Document, Job
from doctypes import registry
from ocr.base import OcrOutput

from .conftest import FakeExtractor

SPEC = {
    "name": "till",
    "matches": {"keywords": ["ticket", "total"], "min_score": 0.5},
    "fields": {"total": {"type": "money", "description": "The total."}},
}


class _Engine:
    name = "fake"

    def read(self, data, mime_type):
        return OcrOutput(
            full_text="TICKET 7\nTOTAL 4.20", blocks=[], engine="fake",
            engine_version="1", params={}, mean_confidence=None,
        )


@pytest.fixture(autouse=True)
def _fakes():
    registry.clear_cache()
    app.dependency_overrides[processors.get_extractor] = lambda: FakeExtractor({"total": "4.20"})
    app.dependency_overrides[processors.get_ocr_engine] = _Engine
    yield
    app.dependency_overrides.pop(processors.get_extractor, None)
    app.dependency_overrides.pop(processors.get_ocr_engine, None)
    registry.clear_cache()


def _draft(client, **body):
    return client.post("/processors", json={"definition": SPEC, **body})


def test_the_whole_lifecycle(client):
    created = _draft(client)
    assert created.status_code == 201
    assert created.json()["created_by"] == "key:test suite"

    published = client.post("/processors/till/versions/1/publish")
    assert published.status_code == 200
    assert published.json()["is_active"] is True

    listed = client.get("/processors").json()
    assert listed == [{
        "name": "till", "display_name": None, "status": "active",
        "active_version": 1, "latest_published": 1, "drafts": [],
    }]

    assert client.post("/processors/till/retire").json()["is_active"] is False
    assert client.get("/processors").json()[0]["status"] == "retired"


def test_yaml_and_json_are_the_same_processor(client):
    response = client.post("/processors", json={"yaml": yaml.safe_dump(SPEC)})
    assert response.status_code == 201
    assert response.json()["definition"]["fields"] == {
        "total": {"type": "money", "description": "The total.", "required": False,
                  "item_fields": None, "choices": None},
    }


def test_sending_both_or_neither_is_refused(client):
    assert client.post("/processors", json={}).status_code == 422
    both = {"definition": SPEC, "yaml": yaml.safe_dump(SPEC)}
    assert client.post("/processors", json=both).status_code == 422


def test_a_seed_file_reference_is_explained_not_ignored(client):
    spec = SPEC | {"transform": {"file": "till.py"}}
    response = client.post("/processors", json={"yaml": yaml.safe_dump(spec)})
    assert response.status_code == 422
    assert "only works in seed files" in response.json()["detail"]


def test_an_invalid_processor_comes_back_with_its_problems(client):
    response = client.post("/processors", json={"definition": SPEC | {"fields": {}}})
    assert response.status_code == 422
    assert response.json()["problems"]


def test_a_lost_update_is_a_409_naming_the_version_to_merge_from(client):
    _draft(client)
    client.post("/processors/till/versions/1/publish")
    client.post("/processors", json={"definition": SPEC, "based_on_version": 1})
    client.post("/processors", json={"definition": SPEC, "based_on_version": 1})
    client.post("/processors/till/versions/3/publish")

    response = client.post("/processors/till/versions/2/publish")
    assert response.status_code == 409
    assert response.json()["latest_version"] == 3


def test_unknown_things_are_404(client):
    assert client.get("/processors/nothing").status_code == 404
    assert client.get("/processors/nothing/versions/1").status_code == 404


def test_a_published_version_cannot_be_overwritten(client):
    _draft(client)
    client.post("/processors/till/versions/1/publish")
    response = client.put("/processors/till/versions/1", json={"definition": SPEC})
    assert response.status_code == 422


def test_a_trial_on_text(client):
    _draft(client)
    response = client.post(
        "/processors/till/versions/1/trial", json={"text": "TICKET 7\nTOTAL 4.20"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["routed_to"] == "till"
    assert body["data"] == {"total": "4.20"}


def test_a_trial_on_an_uploaded_sample_is_read_and_not_ingested(client, session, png_bytes):
    """Ingesting a sample would deduplicate the real copy away when it arrives."""
    _draft(client)
    response = client.post(
        "/processors/till/versions/1/trial/file",
        files={"file": ("sample.png", png_bytes, "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["chars"] == len("TICKET 7\nTOTAL 4.20")
    assert session.query(Document).count() == 0
    assert session.query(Job).count() == 0


def test_it_needs_a_key_like_everything_else(anon_client):
    assert anon_client.get("/processors").status_code == 401
