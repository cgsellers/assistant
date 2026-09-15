from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Not needed until phase 2 (extraction). Optional so the API and worker can
    # start without a key; the ChatProvider raises at first use if it's missing.
    anthropic_api_key: str | None = None

    database_url: str = "sqlite:///./data/ledgerline.db"
    storage_dir: Path = Path("./data/files")
    ocr_service_url: str = "http://localhost:8001"


@lru_cache
def get_settings() -> Settings:
    return Settings()