"""
Shared fixtures.

The important one is `settings`: it repoints the whole application at a
throwaway database and file store under pytest's tmp_path, so no test can
touch ./data/. Everything else builds on it.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from api.main import app
from core.config import Settings, get_settings
from core.db import create_db_engine, get_session
from core.models import Base
from ocr.base import OcrError, OcrOutput, TextBlock

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
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "files"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def db_engine(settings) -> Iterator[Engine]:
    """
    A fresh schema per test.

    create_all rather than running migrations: it is far faster, and the
    migration is separately checked for drift by `alembic check` in CI.
    """
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def client(session_factory) -> Iterator[TestClient]:
    """
    TestClient wired to the sandbox database.

    dependency_overrides replaces get_session, which is why every route takes
    its session by injection rather than importing SessionLocal directly.
    """

    def _override() -> Iterator[Session]:
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


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
