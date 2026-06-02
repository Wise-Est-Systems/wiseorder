from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

from configs.logging import bind_workflow, current_workflow_id, get_logger
from configs.settings import get_settings
from core.approvals.gateway import ApprovalRequest, get_gateway
from core.memory.db import session_scope
from core.memory.models import Task, Workflow


log = get_logger(__name__)

_HN_ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
_REDDIT_SEARCH = "https://www.reddit.com/search.json"

# Default project keywords if none provided in payload.
_DEFAULT_KEYWORDS = [
    "wiseorder",
    "winstack",
    "WISEATA",
    "wisedigest",
    ".win file",
    "wise-est",
]


@dataclass
class MentionHit:
    source: str  # "hn" | "reddit" | "mastodon"
    keyword: str
    external_id: str
    external_url: str
    author: str | None
    title: str | None
    body: str
    posted_at: str | None = None  # ISO-8601 string if known

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "keyword": self.keyword,
            "external_id": self.external_id,
            "external_url": self.external_url,
            "author": self.author,
            "title": self.title,
            "body": self.body,
            "posted_at": self.posted_at,
        }


@dataclass
class MentionResult:
    hits: list[MentionHit] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


async def run_mention_monitor_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
    """Polls public search endpoints for project keywords; creates one
    approval card per new hit.

    Payload schema:
        keywords     : list[str]  (optional, defaults to _DEFAULT_KEYWORDS)
        sources      : list[str]  (optional, defaults to ['hn', 'reddit', 'mastodon'])
        hn_min_age_h : int        (optional, default 0)  — skip hits older than now()-hn_min_age_h

    Returns the count of new hits surfaced and the approval_ids created.
    """
    keywords = list(payload.get("keywords") or _DEFAULT_KEYWORDS)
    sources = list(payload.get("sources") or ["hn", "reddit", "mastodon"])

    async with session_scope() as session:
        wf = Workflow(
            workflow_name="mention_monitor_pipeline",
            task_chain=["poll_sources", "filter_known", "request_approval"],
            status="running",
            logs=[
                _log_entry(
                    "workflow_started",
                    {"keywords": keywords, "sources": sources},
                )
            ],
        )
        session.add(wf)
        await session.flush()
        workflow_id = wf.id

    token = bind_workflow(workflow_id)
    try:
        return await _run_monitor(
            workflow_id=workflow_id, keywords=keywords, sources=sources
        )
    finally:
        current_workflow_id.reset(token)


async def _run_monitor(
    *, workflow_id: int, keywords: list[str], sources: list[str]
) -> dict[str, Any]:
    poll_task_id = await _create_task(
        "poll_sources", workflow_id, {"keywords": keywords, "sources": sources}
    )
    result = MentionResult()

    for keyword in keywords:
        if "hn" in sources:
            try:
                hits = await _search_hn(keyword)
                result.hits.extend(hits)
            except Exception as exc:
                result.errors[f"hn:{keyword}"] = str(exc)
        if "reddit" in sources:
            try:
                hits = await _search_reddit(keyword)
                result.hits.extend(hits)
            except Exception as exc:
                result.errors[f"reddit:{keyword}"] = str(exc)
        if "mastodon" in sources:
            try:
                hits = await _search_mastodon(keyword)
                result.hits.extend(hits)
            except Exception as exc:
                result.errors[f"mastodon:{keyword}"] = str(exc)

    await _complete_task(
        poll_task_id,
        {
            "hits": [h.to_dict() for h in result.hits],
            "errors": result.errors,
            "hit_count": len(result.hits),
        },
    )

    if not result.hits:
        await _complete_workflow(workflow_id, reason="no_hits")
        return {
            "workflow_id": workflow_id,
            "approvals": [],
            "hits": 0,
            "errors": result.errors,
        }

    appr_task_id = await _create_task(
        "request_approval", workflow_id, {"hit_count": len(result.hits)}
    )
    approval_ids: list[int] = []
    try:
        gateway = get_gateway()
        for hit in result.hits:
            req = _approval_from_hit(
                hit=hit, workflow_id=workflow_id, task_id=appr_task_id
            )
            approval_id = await gateway.send(req)
            approval_ids.append(approval_id)
        await _complete_task(appr_task_id, {"approval_ids": approval_ids})
    except Exception as exc:
        await _fail_task(appr_task_id, str(exc))
        await _fail_workflow(workflow_id, f"approval failed: {exc}")
        raise

    await _complete_workflow(workflow_id)
    return {
        "workflow_id": workflow_id,
        "approvals": approval_ids,
        "hits": len(result.hits),
        "errors": result.errors,
    }


async def _search_hn(keyword: str) -> list[MentionHit]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            _HN_ALGOLIA_SEARCH,
            params={"query": keyword, "tags": "story,comment", "hitsPerPage": 30},
        )
        r.raise_for_status()
        data = r.json()
    hits: list[MentionHit] = []
    for h in data.get("hits", []):
        external_id = str(h.get("objectID") or "")
        if not external_id:
            continue
        url = h.get("url") or f"https://news.ycombinator.com/item?id={external_id}"
        hits.append(
            MentionHit(
                source="hn",
                keyword=keyword,
                external_id=external_id,
                external_url=url,
                author=h.get("author"),
                title=h.get("title"),
                body=h.get("comment_text") or h.get("story_text") or "",
                posted_at=h.get("created_at"),
            )
        )
    return hits


async def _search_reddit(keyword: str) -> list[MentionHit]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            _REDDIT_SEARCH,
            params={"q": keyword, "limit": 25, "sort": "new"},
            headers={"User-Agent": "wiseorder-mention-monitor/0.1"},
        )
        r.raise_for_status()
        data = r.json()
    hits: list[MentionHit] = []
    for child in (data.get("data") or {}).get("children", []):
        post = child.get("data") or {}
        external_id = str(post.get("id") or "")
        if not external_id:
            continue
        permalink = post.get("permalink") or ""
        url = f"https://www.reddit.com{permalink}" if permalink else (post.get("url") or "")
        hits.append(
            MentionHit(
                source="reddit",
                keyword=keyword,
                external_id=external_id,
                external_url=url,
                author=post.get("author"),
                title=post.get("title"),
                body=post.get("selftext") or "",
                posted_at=str(post.get("created_utc") or ""),
            )
        )
    return hits


async def _search_mastodon(keyword: str) -> list[MentionHit]:
    s = get_settings()
    if not s.mastodon_instance_url:
        return []
    headers = {}
    if s.mastodon_access_token:
        headers["Authorization"] = f"Bearer {s.mastodon_access_token}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{s.mastodon_instance_url.rstrip('/')}/api/v2/search",
            params={"q": keyword, "type": "statuses", "limit": 20},
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
    hits: list[MentionHit] = []
    for st in data.get("statuses", []):
        external_id = str(st.get("id") or "")
        if not external_id:
            continue
        account = st.get("account") or {}
        hits.append(
            MentionHit(
                source="mastodon",
                keyword=keyword,
                external_id=external_id,
                external_url=str(st.get("url") or st.get("uri") or ""),
                author=account.get("acct"),
                title=None,
                body=_strip_html(st.get("content") or ""),
                posted_at=st.get("created_at"),
            )
        )
    return hits


def _strip_html(s: str) -> str:
    out: list[str] = []
    in_tag = False
    for ch in s:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            continue
        if not in_tag:
            out.append(ch)
    return "".join(out).strip()


def _approval_from_hit(
    *, hit: MentionHit, workflow_id: int, task_id: int
) -> ApprovalRequest:
    title_part = hit.title or "(no title)"
    summary = f"[mention:{hit.source}] '{hit.keyword}' — {title_part[:80]}"
    return ApprovalRequest(
        summary=summary,
        outputs={
            "source": hit.source,
            "keyword": hit.keyword,
            "author": hit.author,
            "title": hit.title,
            "body_preview": (hit.body or "")[:1000],
            "url": hit.external_url,
            "posted_at": hit.posted_at,
        },
        affected=[f"url: {hit.external_url}"] if hit.external_url else [],
        risk_level="low",
        workflow_id=workflow_id,
        task_id=task_id,
    )


async def _create_task(
    type_: str, workflow_id: int, payload: dict[str, Any]
) -> int:
    async with session_scope() as session:
        t = Task(
            type=type_,
            status="running",
            payload=payload,
            workflow_id=workflow_id,
            started_at=datetime.now(timezone.utc),
        )
        session.add(t)
        await session.flush()
        return t.id


async def _complete_task(task_id: int, result: dict[str, Any]) -> None:
    async with session_scope() as session:
        t = (
            await session.execute(select(Task).where(Task.id == task_id))
        ).scalar_one()
        t.status = "completed"
        t.result = result
        t.completed_at = datetime.now(timezone.utc)


async def _fail_task(task_id: int, error: str) -> None:
    async with session_scope() as session:
        t = (
            await session.execute(select(Task).where(Task.id == task_id))
        ).scalar_one()
        t.status = "failed"
        t.error = error
        t.completed_at = datetime.now(timezone.utc)


async def _fail_workflow(workflow_id: int, error: str) -> None:
    async with session_scope() as session:
        wf = (
            await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        ).scalar_one()
        wf.status = "failed"
        wf.logs = list(wf.logs) + [_log_entry("workflow_failed", {"error": error})]
        wf.completed_at = datetime.now(timezone.utc)


async def _complete_workflow(workflow_id: int, *, reason: str | None = None) -> None:
    async with session_scope() as session:
        wf = (
            await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        ).scalar_one()
        wf.status = "completed"
        wf.completed_at = datetime.now(timezone.utc)
        wf.logs = list(wf.logs) + [
            _log_entry("workflow_completed", {"reason": reason} if reason else {})
        ]


def _log_entry(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "data": data,
    }
