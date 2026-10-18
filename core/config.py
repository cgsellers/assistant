from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Optional so the API and worker start without a key. The extractor builds
    # its client at first use and raises there, so only the stage that actually
    # calls the model is affected by a missing key.
    anthropic_api_key: str | None = None

    # The cheap tier does extraction; the strong tier is phase 4's vision
    # fallback. Configured rather than hard-coded because the phase 3 eval
    # harness has to run the same documents through different tiers.
    extraction_model: str = "claude-haiku-4-5"

    database_url: str = "sqlite:///./data/ledgerline.db"
    storage_dir: Path = Path("./data/files")
    ocr_service_url: str = "http://localhost:8001"
    # "ppocr" runs the PP-OCR models in the ocr container; "tesseract"
    # runs in-process and is the test double. See docs/DESIGN.md 5.12.
    ocr_engine: str = "ppocr"

    # Uploads are untrusted input. A phone photo of a receipt is ~1-5 MB and a
    # multi-page PDF invoice rarely exceeds 10 MB, so 20 MB is generous.
    max_upload_bytes: int = 20 * 1024 * 1024

    # Allowlist, not a blocklist: anything not named here is rejected. The OCR
    # container will still treat these as hostile -- this only keeps obvious
    # junk out of storage.
    allowed_mime_types: tuple[str, ...] = (
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/tiff",
        "application/pdf",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()