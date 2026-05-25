from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

from agents.engineering.summarizer import _llm_call
from configs.logging import get_logger
from configs.settings import get_settings


log = get_logger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "social_post.md"


@dataclass
class SocialDraft:
    post: str

    def to_dict(self) -> dict:
        return asdict(self)


class SocialDrafter:
    def __init__(self, model: str | None = None) -> None:
        s = get_settings()
        self.model = model or s.llm_model
        self.timeout = s.llm_timeout_seconds
        self.max_retries = s.llm_max_retries
        self._template = PROMPT_PATH.read_text(encoding="utf-8")

    async def draft(self, *, summary: str, changelog: str, risk_level: str) -> SocialDraft:
        prompt = (
            self._template.replace("{{summary}}", summary)
            .replace("{{changelog}}", changelog)
            .replace("{{risk_level}}", risk_level)
        )
        content = await _llm_call(
            model=self.model,
            prompt=prompt,
            temperature=0.3,
            max_tokens=320,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        content = content.strip('"').strip("'").strip()
        if len(content) > 280:
            content = content[:277] + "..."
        return SocialDraft(post=content)
