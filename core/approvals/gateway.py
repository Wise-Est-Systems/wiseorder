from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from configs.logging import get_logger
from configs.settings import get_settings
from core.memory.db import session_scope
from core.memory.models import Approval


log = get_logger(__name__)


@dataclass
class ApprovalRequest:
    summary: str
    outputs: dict[str, Any]
    affected: list[str] = field(default_factory=list)
    risk_level: str = "low"
    workflow_id: int | None = None
    task_id: int | None = None


class ApprovalGateway:
    """Sends approval requests via Discord webhook or Telegram; always writes to file.

    Local file at logs/approvals.jsonl is the source of truth — webhook delivery
    is best-effort. Decision is recorded back via the FastAPI dashboard.
    """

    def __init__(self) -> None:
        s = get_settings()
        self.discord_url = s.discord_webhook_url
        self.telegram_token = s.telegram_bot_token
        self.telegram_chat_id = s.telegram_chat_id
        self.file_path = Path(s.log_dir) / "approvals.jsonl"

    async def send(self, req: ApprovalRequest) -> int:
        """Persist approval, dispatch notifications, return approval id."""
        async with session_scope() as session:
            row = Approval(
                workflow_id=req.workflow_id,
                task_id=req.task_id,
                summary=req.summary,
                outputs=req.outputs,
                affected=req.affected,
                risk_level=req.risk_level,
                delivered_via=None,
            )
            session.add(row)
            await session.flush()
            approval_id = row.id

        delivered: list[str] = []

        record = {
            "approval_id": approval_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "summary": req.summary,
            "outputs": req.outputs,
            "affected": req.affected,
            "risk_level": req.risk_level,
            "workflow_id": req.workflow_id,
            "task_id": req.task_id,
        }
        try:
            with self.file_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            delivered.append("file")
        except Exception as e:
            log.error({"msg": "approval_file_write_failed", "err": str(e)})

        async with httpx.AsyncClient(timeout=10) as client:
            if self.discord_url:
                try:
                    body = _format_discord(record)
                    r = await client.post(self.discord_url, json=body)
                    if r.status_code < 300:
                        delivered.append("discord")
                    else:
                        log.error(
                            {
                                "msg": "discord_delivery_failed",
                                "status": r.status_code,
                                "body": r.text[:500],
                            }
                        )
                except Exception as e:
                    log.error({"msg": "discord_delivery_error", "err": str(e)})

            if self.telegram_token and self.telegram_chat_id:
                try:
                    url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
                    r = await client.post(
                        url,
                        json={
                            "chat_id": self.telegram_chat_id,
                            "text": _format_telegram(record),
                            "parse_mode": "Markdown",
                        },
                    )
                    if r.status_code < 300:
                        delivered.append("telegram")
                    else:
                        log.error(
                            {
                                "msg": "telegram_delivery_failed",
                                "status": r.status_code,
                                "body": r.text[:500],
                            }
                        )
                except Exception as e:
                    log.error({"msg": "telegram_delivery_error", "err": str(e)})

        async with session_scope() as session:
            res = await session.execute(select(Approval).where(Approval.id == approval_id))
            row = res.scalar_one()
            row.delivered_via = ",".join(delivered) if delivered else None

        log.info({"msg": "approval_sent", "id": approval_id, "delivered": delivered})
        return approval_id

    async def decide(self, approval_id: int, decision: str) -> bool:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be 'approved' or 'rejected'")
        async with session_scope() as session:
            res = await session.execute(select(Approval).where(Approval.id == approval_id))
            row = res.scalar_one_or_none()
            if row is None:
                return False
            row.decision = decision
            row.decided_at = datetime.now(timezone.utc)
        return True


def _format_discord(record: dict[str, Any]) -> dict[str, Any]:
    affected = "\n".join(f"• {p}" for p in record["affected"][:10]) or "(none listed)"
    outputs = record["outputs"]
    eng = outputs.get("engineering_summary", "")
    social = outputs.get("social_post", "")
    fields = []
    if eng:
        fields.append({"name": "Engineering summary", "value": _trim(eng, 1024), "inline": False})
    if social:
        fields.append({"name": "Social draft", "value": _trim(social, 1024), "inline": False})
    fields.append({"name": "Affected files", "value": _trim(affected, 1024), "inline": False})
    return {
        "username": "WiseOrder",
        "embeds": [
            {
                "title": f"Approval #{record['approval_id']} — {record['risk_level'].upper()}",
                "description": _trim(record["summary"], 2048),
                "color": _risk_color(record["risk_level"]),
                "fields": fields,
                "timestamp": record["ts"],
            }
        ],
    }


def _format_telegram(record: dict[str, Any]) -> str:
    outputs = record["outputs"]
    eng = outputs.get("engineering_summary", "")
    social = outputs.get("social_post", "")
    parts = [
        f"*Approval #{record['approval_id']}* — `{record['risk_level']}`",
        "",
        record["summary"],
    ]
    if eng:
        parts.append("")
        parts.append("*Engineering summary*")
        parts.append(_trim(eng, 1500))
    if social:
        parts.append("")
        parts.append("*Social draft*")
        parts.append(_trim(social, 1000))
    if record["affected"]:
        parts.append("")
        parts.append("*Affected files*")
        parts.append("\n".join(f"• {p}" for p in record["affected"][:10]))
    return "\n".join(parts)[:4000]


def _trim(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def _risk_color(risk: str) -> int:
    return {"low": 0x2ECC71, "medium": 0xF1C40F, "high": 0xE74C3C}.get(risk, 0x95A5A6)


_gateway: ApprovalGateway | None = None


def get_gateway() -> ApprovalGateway:
    global _gateway
    if _gateway is None:
        _gateway = ApprovalGateway()
    return _gateway
