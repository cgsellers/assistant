"""
Values longer than their columns.

SQLite ignores VARCHAR lengths; Postgres enforces them, so an over-long value
there is a failed insert and a 500. These tests send the longest realistic
input through each path that writes a bounded column. On SQLite they check
the value was cut or refused; under TEST_DATABASE_URL they also prove
Postgres accepts what is written.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core import auth, repository, users
from core.models import Document
from doctypes import authoring
from doctypes.definition import Definition

from .conftest import TINY_PNG, receipt_definition


def test_an_uploaders_overlong_filename_is_cut_keeping_its_extension(session):
    name = "scan-" + "x" * 400 + ".pdf"
    document, _ = repository.ingest(
        session, data=TINY_PNG, mime_type="image/png", original_filename=name
    )
    session.expire_all()
    stored = session.get(Document, document.id).original_filename
    assert len(stored) == repository.FILENAME_LIMIT
    assert stored.endswith("x.pdf")


def test_a_normal_filename_is_left_alone(session):
    document, _ = repository.ingest(
        session, data=TINY_PNG, mime_type="image/png", original_filename="receipt.jpg"
    )
    assert document.original_filename == "receipt.jpg"


def test_a_long_hostname_still_makes_a_worker_id_that_fits(monkeypatch):
    from worker import run as worker

    monkeypatch.setattr(worker.socket, "gethostname", lambda: "h" * 300)
    identity = worker.worker_id()
    assert len(identity) <= 100
    assert identity.endswith(f":{worker.os.getpid()}")


def test_a_worker_with_that_id_can_still_claim(session, monkeypatch):
    from worker import run as worker

    monkeypatch.setattr(worker.socket, "gethostname", lambda: "h" * 300)
    repository.ingest(session, data=TINY_PNG, mime_type="image/png", original_filename="a.png")
    assert worker.claim(session, me=worker.worker_id()) is not None


def test_a_display_name_too_long_for_its_column_is_refused():
    with pytest.raises(ValidationError, match="at most 200"):
        receipt_definition(display_name="R" * 201)


def test_a_profile_name_too_long_for_its_column_is_refused():
    with pytest.raises(ValidationError, match="longer than 100"):
        receipt_definition(profiles={"p" * 101: {"tax_ids": ["A1"]}})


def test_the_longest_email_fits_in_created_by_with_the_ai_suffix(session, fast_hashing):
    email = ("a" * 64) + "@" + ("b" * 185) + ".com"
    assert len(email) == 254
    users.create(session, email, "correct horse battery")
    row = authoring.create_draft(
        session, receipt_definition(), based_on=None,
        created_by=f"user:{email} (ai draft)",
    )
    session.expire_all()
    assert authoring.get_version(session, "receipt", row.version).created_by.endswith("(ai draft)")


def test_an_overlong_email_or_key_name_is_refused_with_a_reason(session, fast_hashing):
    with pytest.raises(ValueError, match="at most 254"):
        users.create(session, "a" * 250 + "@x.com", "correct horse battery")
    with pytest.raises(ValueError, match="at most 200"):
        auth.create(session, "k" * 201)


def test_every_shipped_processor_fits_its_columns():
    from doctypes import seed

    for definition in seed.load_seeds():
        Definition.model_validate(definition.model_dump())
