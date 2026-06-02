from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import litellm
from litellm import acompletion

from configs.logging import get_logger
from configs.settings import get_settings


log = get_logger(__name__)


HN_SYSTEM = (
    "You draft Show HN posts for an infrastructure project. Voice: clean, "
    "declarative, no manifesto, no hype, no marketing copy. Respond ONLY with "
    "valid JSON matching the schema described in the user message."
)

EMAIL_SYSTEM = (
    "You draft 1:1 cold outreach emails for an infrastructure project. "
    "Voice: short, professional, lowkey. No manifesto. Never reference "
    "private personal motives. Respond ONLY with valid JSON matching the "
    "schema described in the user message."
)

POST_SYSTEM = (
    "You draft public posts for an infrastructure project's social and "
    "blog presence. Voice: clean, declarative, no manifesto, no hype, no "
    "marketing copy. Never reference private personal motives. Respond "
    "ONLY with valid JSON matching the schema described in the user message."
)


class OutreachDrafter:
    """Draft generator for the distribution pipeline.

    draft_hn_post(event_type, payload) -> (title, first_comment, url)
    draft_email(recipient, event_type, payload) -> (subject, body)
    """

    def __init__(self, model: str | None = None) -> None:
        s = get_settings()
        self.model = model or s.distribution_drafter_model or s.llm_model
        self.timeout = s.llm_timeout_seconds
        self.max_retries = s.llm_max_retries

    async def draft_hn_post(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> tuple[str, str, str]:
        url = str(payload.get("url") or "").strip()
        if not url:
            raise ValueError("draft_hn_post: payload['url'] is required")
        user_prompt = (
            "Draft a Show HN post for the artifact below.\n\n"
            f"event_type: {event_type}\n"
            f"url:        {url}\n"
            f"summary:    {payload.get('summary', '')}\n"
            f"context:    {payload.get('context', '')}\n\n"
            "Return JSON with exactly these keys:\n"
            "  title         — string, <=80 chars, starts with 'Show HN: '\n"
            "  first_comment — string, <=2000 chars, plain text, no Markdown\n"
            "\nThe first_comment will be posted into the submission thread "
            "immediately after submitting. Use it to give context, link to "
            "the source repo, and disclose non-goals."
        )
        data = await self._json_call(system=HN_SYSTEM, user=user_prompt, max_tokens=1024)
        return (
            str(data.get("title", "")).strip(),
            str(data.get("first_comment", "")).strip(),
            url,
        )

    async def draft_email(
        self,
        *,
        recipient: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> tuple[str, str]:
        user_prompt = (
            "Draft a 1:1 cold-reach email to the named recipient.\n\n"
            f"recipient:  {recipient}\n"
            f"event_type: {event_type}\n"
            f"context:    {payload.get('context', '')}\n"
            f"ask:        {payload.get('ask', 'Would you take a look?')}\n"
            f"url:        {payload.get('url', '')}\n\n"
            "Return JSON with exactly these keys:\n"
            "  subject — string, <=100 chars\n"
            "  body    — string, <=2000 chars, plain text, no Markdown, "
            "no salutations longer than one line, no signature block (the "
            "operator appends their own signature on send)."
        )
        data = await self._json_call(system=EMAIL_SYSTEM, user=user_prompt, max_tokens=1024)
        return (
            str(data.get("subject", "")).strip(),
            str(data.get("body", "")).strip(),
        )

    async def draft_mastodon_post(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> tuple[str, str]:
        url = str(payload.get("url") or "").strip()
        if not url:
            raise ValueError("draft_mastodon_post: payload['url'] is required")
        user_prompt = (
            "Draft a Mastodon post (toot) for the artifact below.\n\n"
            f"event_type: {event_type}\n"
            f"url:        {url}\n"
            f"summary:    {payload.get('summary', '')}\n"
            f"context:    {payload.get('context', '')}\n\n"
            "Return JSON with exactly these keys:\n"
            "  body — string, <=480 chars (leave room for url + ellipsis), plain text, no markdown. "
            "Include the url somewhere in the body. Use 1-3 relevant hashtags maximum at the end "
            "(e.g. #cryptography #ai). No CW spoiler tags unless the content is genuinely sensitive."
        )
        data = await self._json_call(system=POST_SYSTEM, user=user_prompt, max_tokens=512)
        return (str(data.get("body", "")).strip(), url)

    async def draft_blog_post(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> tuple[str, str, str]:
        """Returns (title, body_markdown, canonical_url)."""
        canonical_url = str(payload.get("url") or "").strip()
        if not canonical_url:
            raise ValueError("draft_blog_post: payload['url'] (canonical_url) is required")
        user_prompt = (
            "Draft a long-form engineering blog post for the artifact below. The post will "
            "be cross-published with a canonical-url pointer back to the source URL so this "
            "platform does NOT outrank the source in search.\n\n"
            f"event_type: {event_type}\n"
            f"canonical_url: {canonical_url}\n"
            f"summary:       {payload.get('summary', '')}\n"
            f"context:       {payload.get('context', '')}\n\n"
            "Return JSON with exactly these keys:\n"
            "  title         — string, <=80 chars, no clickbait, declarative\n"
            "  body_markdown — string, 400-1200 words, valid Markdown. Sections: a 1-paragraph "
            "lede that states what was built and why a reader should care, a 'How it works' "
            "section with at most one code block, a 'What it does NOT do' section listing "
            "non-goals honestly, a closing one-paragraph 'Where to look next' with links."
        )
        data = await self._json_call(system=POST_SYSTEM, user=user_prompt, max_tokens=4096)
        return (
            str(data.get("title", "")).strip(),
            str(data.get("body_markdown", "")).strip(),
            canonical_url,
        )

    async def _json_call(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        content = await _llm_call(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        return _parse_json(content)


async def _llm_call(
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout: float,
    max_retries: int,
) -> str:
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await acompletion(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            return resp["choices"][0]["message"]["content"]
        except (litellm.exceptions.APIConnectionError, litellm.exceptions.Timeout) as exc:
            last_err = exc
            if attempt < max_retries:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            raise
    raise RuntimeError(f"LLM call exhausted retries: {last_err!r}")


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_json(content: str) -> dict[str, Any]:
    """Extract a JSON object from the LLM response, tolerating ``` fences."""
    s = content.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    match = _JSON_FENCE_RE.search(s)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Last resort: find the outermost {...}
    first = s.find("{")
    last = s.rfind("}")
    if first != -1 and last != -1 and last > first:
        return json.loads(s[first : last + 1])
    raise ValueError(f"could not parse JSON from LLM response: {s[:200]!r}")
