"""
Shared fixtures.

The important one is `settings`: it repoints the whole application at a
throwaway database and file store under pytest's tmp_path, so no test can
touch ./data/. Everything else builds on it.

The suite runs on SQLite by default: a fresh file per test, no service to
start. Set TEST_DATABASE_URL to run all of it against Postgres instead --

    TEST_DATABASE_URL=postgresql+psycopg://ledgerline:ledgerline@127.0.0.1:5432/ledgerline_test

-- which is what CI does, because the two differ in exactly the ways that
matter here: Postgres enforces column lengths, has real timestamps and exact
decimals, and locks rows. The schema is built once per run there and every
table emptied between tests; building it per test would take minutes.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from api.main import app
from core.config import Settings, get_settings
from core.db import create_db_engine, get_session
from core.models import Base
from doctypes.definition import Definition
from llm.base import ExtractionResult
from ocr.base import OcrError, OcrOutput, TextBlock

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
ON_POSTGRES = bool(TEST_DATABASE_URL)

#: For the few tests about SQLite's own quirks -- its foreign keys being off
#: by default, its naive datetimes -- which have nothing to test on Postgres.
sqlite_only = pytest.mark.skipif(ON_POSTGRES, reason="tests a SQLite-specific behaviour")

# --- A real 1x1 PNG, for tests that need valid bytes but no actual text ---
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Iterator[Settings]:
    """
    Point configuration at a per-test sandbox.

    get_settings is lru_cached, so the cache is cleared on both sides: once so
    this test sees the override, and once afterwards so the next test does not
    inherit a tmp_path that has since been deleted.
    """
    monkeypatch.setenv(
        "DATABASE_URL", TEST_DATABASE_URL or f"sqlite:///{tmp_path / 'test.db'}"
    )
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "files"))
    # Pinned, because a developer's .env may relax them for local work -- and
    # the tests are what prove the production values behave.
    monkeypatch.setenv("REQUIRE_AUTH", "true")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def _postgres_engine() -> Iterator[Engine]:
    """One schema for the whole run, rebuilt at the start so it matches the models."""
    engine = create_db_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_engine(settings, request) -> Iterator[Engine]:
    """
    A fresh schema per test: a new SQLite file, or empty Postgres tables.

    create_all rather than running migrations: it is far faster, and the
    migration is separately checked for drift by `alembic check` in CI.
    """
    if ON_POSTGRES:
        engine = request.getfixturevalue("_postgres_engine")
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        with engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        yield engine
        return
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory) -> Iterator[Session]:
    """
    Sees every organisation, as the worker does. Tests about who sees what
    open sessions of their own with narrower scopes; requests made through
    the API get theirs from their credentials, as in production.
    """
    from core.scoping import Scope, set_scope

    with session_factory() as s:
        set_scope(s, Scope.everyone("test"))
        yield s


@contextmanager
def system_session(factory) -> Iterator[Session]:
    """A session from `factory` that sees every organisation, as the worker's does."""
    from core.scoping import Scope, set_scope

    with factory() as s:
        set_scope(s, Scope.everyone("test"))
        yield s


def demo_org_id(session) -> str:
    """The team's organisation, which most tests' documents, keys and users belong to."""
    from core import tenancy

    return tenancy.demo_organisation(session).id


@pytest.fixture
def api_key(session_factory) -> str:
    """
    A real key in the sandbox database. Returned as the secret.

    Admin, so API tests can exercise publishing; the role checks themselves
    are tested with keys and users made for the purpose.
    """
    from core import auth

    with session_factory() as s:
        _, secret = auth.create(s, "test suite", role="admin", organisation_id=demo_org_id(s))
    return secret


@pytest.fixture
def _wired(session_factory) -> Iterator[None]:
    """Point the app's session dependency at the sandbox database."""

    def _override() -> Iterator[Session]:
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client(_wired, api_key) -> Iterator[TestClient]:
    """
    TestClient wired to the sandbox database, carrying a real key.

    Authenticated rather than bypassing auth, so every API test exercises the
    path a caller actually takes. `anon_client` is the one that does not.
    """
    with TestClient(app, headers={"Authorization": f"Bearer {api_key}"}) as c:
        yield c


@pytest.fixture
def anon_client(_wired) -> Iterator[TestClient]:
    """No credentials. For the tests that assert the door is shut."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fast_hashing(monkeypatch):
    """
    argon2 is slow on purpose, which is the point in production and a few
    seconds of nothing in a test run. Same algorithm, minimal cost.
    """
    from argon2 import PasswordHasher

    from core import users

    monkeypatch.setattr(users, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    users._dummy_hash.cache_clear()
    yield
    users._dummy_hash.cache_clear()


# --- Fake OCR engines -------------------------------------------------------
# These exist because OcrEngine is a Protocol: the worker can be tested with
# no tesseract binary and no container, which is most of the point of having
# the protocol in the first place.


class FakeEngine:
    """Returns canned output and records how often it was called."""

    name = "fake"

    def __init__(self, output: OcrOutput | None = None, raises: Exception | None = None):
        self.output = output or OcrOutput(
            full_text="TOTAL 9.35",
            blocks=[TextBlock(text="TOTAL 9.35", bbox=(0, 0, 10, 10), confidence=0.9)],
            mean_confidence=0.9,
            engine="fake",
            engine_version="1.0",
            params={"stub": True},
        )
        self.raises = raises
        self.calls: list[tuple[int, str]] = []

    def read(self, data: bytes, mime_type: str) -> OcrOutput:
        self.calls.append((len(data), mime_type))
        if self.raises is not None:
            raise self.raises
        return self.output


@pytest.fixture
def fake_engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def failing_engine() -> FakeEngine:
    return FakeEngine(raises=OcrError("engine exploded"))


# --- Fake extractors --------------------------------------------------------
# Extractor is a Protocol for the same reason OcrEngine is: the extract stage
# is testable with no API key, no network, and no spend.


class FakeExtractor:
    """Returns canned JSON and records what it was asked to extract."""

    def __init__(
        self,
        data: dict | None = None,
        raises: Exception | None = None,
        model: str = "claude-haiku-4-5-20251001",
    ):
        self.data = {} if data is None else data
        self.raises = raises
        self.model = model
        self.calls: list[dict] = []

    def extract(self, *, json_schema: dict, text: str, doc_type: str) -> ExtractionResult:
        self.calls.append({"json_schema": json_schema, "text": text, "doc_type": doc_type})
        if self.raises is not None:
            raise self.raises
        # The numbers are the ones a real haiku-4-5 call returned for a receipt
        # of this size, so assertions about cost are against a plausible row.
        return ExtractionResult(
            data=self.data,
            model=self.model,
            input_tokens=790,
            output_tokens=83,
            cache_read_tokens=0,
            cost_usd=Decimal("0.001205"),
            latency_ms=3370,
            stop_reason="end_turn",
            params={"doc_type": doc_type},
        )


class ForbiddenExtractor:
    """
    Fails the test if the model is called at all.

    The default for tests that do not arrange an extraction: reaching the model
    unexpectedly means a real deployment would have spent money there, which
    should be loud rather than silently free.
    """

    model = "forbidden"

    def extract(self, **kwargs) -> ExtractionResult:
        raise AssertionError("extractor was called, but this test did not expect it")


def receipt_definition(**overrides) -> Definition:
    """
    A minimal receipt type that matches the OCR text the fake engines return.

    Deliberately small: tests that care about rules or profiles build their own
    rather than bending this one into every shape.
    """
    spec = {
        "name": "receipt",
        "matches": {"keywords": ["total"], "min_score": 0.3},
        "fields": {
            "total": {
                "type": "money",
                "description": "The amount charged, labelled TOTAL.",
                "required": True,
            },
            "vendor": {"type": "string", "description": "The business name."},
        },
        "rules": [{"type": "positive", "field": "total"}],
    }
    spec.update(overrides)
    return Definition.model_validate(spec)


# --- Image helpers ----------------------------------------------------------


@pytest.fixture
def png_bytes() -> bytes:
    return TINY_PNG


@pytest.fixture
def receipt_png() -> bytes:
    """A rendered receipt, for the tests that run a real OCR engine."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (300, 120), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(["CAFE LUNA", "TOTAL 9.35"]):
        draw.text((10, 20 + i * 40), line, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
