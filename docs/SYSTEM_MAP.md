# SYSTEM_MAP

WiseOrder Runtime v0.1 — event-driven workflow orchestration. Single Python process, one Postgres, one Redis, one embedded ChromaDB. No microservices, no Kubernetes.

## Process model

```
                       single python process
   ┌─────────────────────────────────────────────────────────────────┐
   │                       Orchestrator                              │
   │                                                                 │
   │   N workers (async)         FastAPI server         EventWatcher │
   │   ┌────────┐ ┌────────┐    ┌──────────────┐      ┌────────────┐ │
   │   │ worker0│ │ worker1│    │  uvicorn :8765│      │  Watchdog  │ │
   │   └───┬────┘ └───┬────┘    └───────┬───────┘      │   thread   │ │
   │       └─────┬────┘                 │              └─────┬──────┘ │
   └─────────────┼──────────────────────┼────────────────────┼────────┘
                 │                      │                    │
                 ▼                      ▼                    │
            ┌─────────┐            ┌─────────┐         schedule
            │  Redis  │            │Postgres │       commit_pipeline
            │ (queue) │            │(memory) │              │
            └─────────┘            └─────────┘              │
                 ▲                      ▲                   │
                 │                      │                   │
                 └──────── enqueue ─────┴───────────────────┘
                              ▲
                              │
                       commit detected
```

## Components (subsystem → owner → state → failure → recovery → inspect)

| subsystem | what it does | state owned | how it fails | how it recovers | how to inspect |
|---|---|---|---|---|---|
| **Orchestrator** (`core/orchestrator/main.py`) | runs workers, watcher, API; reaps orphans at startup | in-memory worker list, stop event | crashes propagate from workers/API | startup orphan reaper marks `running` workflows older than `WISEORDER_ORPHAN_WORKFLOW_MAX_AGE_SECONDS` (default 600) as `interrupted` | `python -m core.orchestrator.main --probe-services` |
| **EventWatcher** (`core/events/watcher.py`) | detects git commits in `WISEORDER_WATCH_PATHS` | per-repo `_last_sha` | Watchdog thread can die silently; coroutine schedule fails on closed loop | logged + skipped; init-to-start gap reconciled by `_reconcile_after_start()` | logs: `event_watcher_watching`, `commit_detected`, `watcher_*` |
| **RedisQueue** (`core/queues/redis_queue.py`) | three lists: HIGH / NORMAL / FAILED | jobs as JSON blobs in Redis | Redis restart → blocking reconnect | reconnect handled by `redis.asyncio`; in-flight job is lost (see FAILURE_MODEL.md F1) | `GET /queues/failed`, `GET /health` |
| **Memory** (`core/memory/db.py`, `models.py`) | Postgres: `tasks`, `workflows`, `memory`, `approvals` | SQL rows | DB restart → next query reconnects via `pool_pre_ping=True` | tasks/workflows in `running` past max-age → reaped at next start | `GET /tasks`, `GET /workflows`, `GET /workflows/{id}` |
| **VectorStore** (`core/memory/vector.py`) | embedded persistent ChromaDB at `data/chroma/` | local files | disk full → upsert raises; index corrupt → ChromaDB error | failure logged as `vector_index_failed`; structured memory row still saved | `GET /memory/search?q=...`, `GET /stats` |
| **ApprovalGateway** (`core/approvals/gateway.py`) | persists approval rows; posts to Discord/Telegram; always writes `logs/approvals.jsonl` | Postgres `approvals` rows + JSONL file | network delivery may fail (logged); file/DB writes are blocking | webhook failures are best-effort; file is source of truth | `GET /approvals`, `tail -f logs/approvals.jsonl` |
| **Agents** (`agents/engineering/`, `agents/social/`) | LiteLLM calls with prompts from `prompts/` | none (stateless) | LLM timeout / 5xx → bounded retry then propagate | `WISEORDER_LLM_TIMEOUT_SECONDS` / `WISEORDER_LLM_MAX_RETRIES` | log: `llm_timeout`, `llm_transient` |
| **CommitPipeline** (`workflows/commit_pipeline.py`) | summarize → draft → save → request approval | per-workflow tasks + log entries in Postgres | any step failure marks workflow `failed`; idempotent on `(repo, sha)` | duplicate SHA returns existing workflow_id; crashed mid-run reaped at next start | `GET /workflows/{id}` |
| **API** (`api/server.py`) | dashboard + JSON endpoints | none | uvicorn shutdown is graceful via `should_exit` | restart with `python -m core.orchestrator.main` | `curl :8765/healthz`, `curl :8765/ready` |

## Data flow — one commit, happy path

```
  git commit  →  Watchdog event  →  HEAD SHA changed?  → enqueue commit_pipeline job
                                                                   │
                                                                   ▼
   Postgres workflows row created  ←  worker dequeues  ←  Redis BLPOP returns job
              │
              ├─► task#1 summarize    →  LiteLLM (Anthropic)  →  EngineeringSummary JSON
              ├─► task#2 draft_social →  LiteLLM (Anthropic)  →  SocialDraft (≤280 chars)
              ├─► task#3 save         →  Postgres memory row + Chroma upsert
              └─► task#4 request_approval → file append + Discord/Telegram webhook
                          │
                          ▼
              dashboard polls /approvals?pending=true every 5s
                          │
                          ▼
               operator clicks approve/reject → POST /approvals/{id}/decide
```

## Endpoints (read-only unless noted)

| method | path | purpose |
|---|---|---|
| GET | `/` | HTML dashboard |
| GET | `/health` /  `/healthz` | queue depths + redis ping |
| GET | `/ready` | 200 iff DB + Redis both reachable, else 503 |
| GET | `/stats` | task/workflow/queue counts, vector size |
| GET | `/tasks` | recent tasks (filter `?status=`) |
| GET | `/workflows` | recent workflows (filter `?status=`) |
| GET | `/workflows/{id}` | one workflow + its tasks |
| GET | `/approvals?pending=true` | pending or all approvals |
| GET | `/queues/failed` | dead-letter inspection |
| GET | `/memory/recent` | recent commit summaries |
| GET | `/memory/search?q=...` | vector search |
| GET | `/logs/recent` | recent workflow log entries |
| POST | `/approvals/{id}/decide` | `{"decision": "approved" | "rejected"}` |
| POST | `/trigger` | enqueue a job manually `{"type": ..., "payload": ...}` |

## What is *not* in this system

- No retry-from-checkpoint inside a workflow (a failed step fails the workflow).
- No multi-process workers (run more if needed; idempotency makes this safe at the workflow level only — Redis dedup is not implemented).
- No authentication on the API (bound to `127.0.0.1` by default — do not expose).
- No remote storage of `logs/approvals.jsonl` (rotate or back up manually if you care).
