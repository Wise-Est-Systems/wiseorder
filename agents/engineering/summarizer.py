from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

import litellm
from litellm import acompletion

from configs.logging import get_logger
from configs.settings import get_settings


log = get_logger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "engineering_summary.md"


@dataclass
class EngineeringSummary:
    summary: str
    changed_files: list[str]
    changelog: str
    risk_level: str

    def to_dict(self) -> dict:
        return asdict(self)


class EngineeringSummarizer:
    def __init__(self, model: str | None = None) -> None:
        s = get_settings()
        self.model = model or s.llm_model
        self.timeout = s.llm_timeout_seconds
        self.max_retries = s.llm_max_retries
        self._template = PROMPT_PATH.read_text(encoding="utf-8")

    async def summarize(
        self,
        *,
        subject: str,
        author: str,
        sha: str,
        diff: str,
    ) -> EngineeringSummary:
        prompt = (
            self._template.replace("{{subject}}", subject)
            .replace("{{author}}", author)
            .replace("{{sha}}", sha)
            .replace("{{diff}}", diff)
        )
        content = await _llm_call(
            model=self.model,
            prompt=prompt,
            temperature=0.2,
            max_tokens=1024,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        data = _parse_json(content)

        return EngineeringSummary(
            summary=str(data.get("summary", "")).strip(),
            changed_files=[str(x) for x in data.get("changed_files", [])],
            changelog=str(data.get("changelog", "")).strip(),
            risk_level=_normalize_risk(data.get("risk_level", "low")),
        )


async def _llm_call(
    *,
    model: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    max_retries: int,
) -> str:
    """Bounded LLM call with explicit timeout and bounded retries on transient errors."""
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = await asyncio.wait_for(
                acompletion(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature,
                    max_tokens=max_tokens,
                ),
                timeout=timeout,
            )
            return resp["choices"][0]["message"]["content"].strip()
        except asyncio.TimeoutError:
            log.warning({"msg": "llm_timeout", "model": model, "attempt": attempt, "timeout_s": timeout})
            if attempt > max_retries:
                raise
        except (litellm.RateLimitError, litellm.APIConnectionError, litellm.ServiceUnavailableError) as e:
            log.warning({"msg": "llm_transient", "model": model, "attempt": attempt, "err": str(e)})
            if attempt > max_retries:
                raise
            await asyncio.sleep(min(2 ** attempt, 30))


def _parse_json(content: str) -> dict:
    """Tolerate code fences and surrounding prose."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fenced:
        content = fenced.group(1)
    else:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            content = m.group(0)
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.error({"msg": "summarizer_json_parse_failed", "err": str(e), "content": content[:500]})
        return {
            "summary": content[:500],
            "changed_files": [],
            "changelog": "",
            "risk_level": "low",
        }


def _normalize_risk(v: str) -> str:
    v = str(v).strip().lower()
    return v if v in {"low", "medium", "high"} else "low"
