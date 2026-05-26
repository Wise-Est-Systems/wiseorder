from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated, List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2

    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # NoDecode: prevents pydantic-settings from trying to JSON-decode the
    # raw env string before our validator runs. Lets us accept the simple
    # `WISEORDER_WATCH_PATHS=/a,/b` shape from .env files.
    watch_paths: Annotated[List[str], NoDecode] = Field(default_factory=list)

    api_host: str = "127.0.0.1"
    api_port: int = 8765

    log_level: str = "INFO"
    log_dir: str = str(REPO_ROOT / "logs")

    orphan_workflow_max_age_seconds: int = 600
    dedup_ttl_seconds: int = 3600

    api_allow_remote_bind: bool = False
    api_auth_token: str = ""

    @field_validator("watch_paths", mode="before")
    @classmethod
    def _split_paths(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v


_settings: Settings | None = None
_settings_lock = threading.Lock()


def get_settings() -> Settings:
    global _settings
    if _settings is not None:
        return _settings
    with _settings_lock:
        if _settings is not None:
            return _settings
        s = Settings()
        Path(s.log_dir).mkdir(parents=True, exist_ok=True)
        Path(s.chroma_path).mkdir(parents=True, exist_ok=True)
        _settings = s
        return _settings
