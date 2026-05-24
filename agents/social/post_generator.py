from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

from litellm import acompletion

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
        self.model = model or get_settings().llm_model
        self._template = PROMPT_PATH.read_text(encoding="utf-8")

    async def draft(self, *, summary: str, changelog: str, risk_level: str) -> SocialDraft:
        prompt = (
            self._template.replace("{{summary}}", summary)
            .replace("{{changelog}}", changelog)
            .replace("{{risk_level}}", risk_level)
        )
        resp = await acompletion(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=320,
        )
        content = resp["choices"][0]["message"]["content"].strip()
        content = content.strip('"').strip("'").strip()
        if len(content) > 280:
            content = content[:277] + "..."
        return SocialDraft(post=content)
