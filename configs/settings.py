from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_prefix="WISEORDER_",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://wiseorder:wiseorder@localhost:5433/wiseorder"
    redis_url: str = "redis://localhost:6380/0"
    chroma_path: str = str(REPO_ROOT / "data" / "chroma")

    llm_model: str = "claude-sonnet-4-6"

    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    watch_paths: List[str] = Field(default_factory=list)

    api_host: str = "127.0.0.1"
    api_port: int = 8765

    log_level: str = "INFO"
    log_dir: str = str(REPO_ROOT / "logs")

    @field_validator("watch_paths", mode="before")
    @classmethod
    def _split_paths(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        Path(_settings.log_dir).mkdir(parents=True, exist_ok=True)
        Path(_settings.chroma_path).mkdir(parents=True, exist_ok=True)
    return _settings
