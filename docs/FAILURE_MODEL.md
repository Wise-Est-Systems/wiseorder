# FAILURE_MODEL

Explicit failure model for every subsystem. For each failure: current behavior, expected behavior, residual risk, and the recommended response.

## F1 — Interrupted queue job (worker dies between BLPOP and completion)

| | |
|---|---|
| current | Job was already removed from Redis by BLPOP. Worker died before persisting result. Job is **lost from the queue.** Workflow row (if created) remains `running`. |
| expected | Next orchestrator start reaps the `running` workflow as `interrupted`; the operator can manually re-enqueue via `POST /trigger`. |
| residual risk | The work is not auto-retried. By design — silent retry of LLM-spending work could double-bill on a flapping process. |
| inspect | `GET /workflows?status=interrupted` |
| response | Manually re-enqueue if needed: `curl -X POST :8765/trigger -d '{"type":"commit_pipeline","payload":{...}}'`. |

## F2 — Redis restart

| | |
|---|---|
| current | `redis.asyncio` client auto-reconnects on next call. In-flight `BLPOP` calls raise `ConnectionError`; the worker catches it, logs `worker_dequeue_error`, sleeps 1s, retries. |
| expected | Service self-recovers within ~1–3s. Jobs already in Redis lists survive (AOF persistence on). Jobs popped before restart but not completed are lost — same as F1. |
| residual risk | If Redis loses AOF data (disk corruption), all pending jobs disappear. Watchdog still works; new commits will enqueue new jobs. |
| inspect | `docker compose logs redis`; `GET /health` |
| response | Bring Redis back; verify `GET /health` returns `"redis": true`. |

## F3 — Postgres restart

| | |
|---|---|
| current | `pool_pre_ping=True` validates connections before use; broken connections are replaced. In-flight queries raise; the workflow step that issued them fails and the workflow is marked `failed`. |
| expected | Service self-recovers. The half-committed workflow stays `failed`. |
| residual risk | If a workflow was between `_complete_task` and the next step, the task is marked done but the workflow shows `failed` at the next step. Audit consistent. |
| inspect | `docker compose logs postgres`; `GET /ready` |
| response | Bring Postgres back. Failed workflows can be re-enqueued manually. |

## F4 — Duplicate event (same commit SHA fires twice)

| | |
|---|---|
| current | `run_commit_pipeline` queries for an existing workflow whose first log entry contains the same `(repo, sha)` and status in `(running, completed)`. If found, returns the existing `workflow_id` with `skipped=True`. |
| expected | One workflow per commit. No duplicate Postgres rows, no duplicate approval cards, no duplicate vector entries. |
| residual risk | If the duplicate fires within the same millisecond before either has flushed to Postgres, both could pass the existence check (race window). Workers are serialized per process — this is only a risk under multi-process workers. |
| inspect | `GET /workflows?limit=20` — should see one row per SHA |
| response | None; idempotent by design. |

## F5 — Malformed event payload (missing `sha` or `repo`)

| | |
|---|---|
| current | `_require_keys` raises `ValueError("commit_pipeline payload missing required keys: ...")` before any DB write. Worker catches it, marks the job failed, pushes to dead-letter queue with the error in `notes`. |
| expected | No partial state. Operator sees the malformed job in `/queues/failed`. |
| residual risk | None for missing required keys. Extra keys are ignored. Type confusion (e.g., `sha=None`) would raise a TypeError inside SQLAlchemy — also caught. |
| inspect | `GET /queues/failed` |
| response | Investigate origin (usually a manual `POST /trigger` typo). Discard. |

## F6 — Partial workflow execution (process killed mid-pipeline)

| | |
|---|---|
| current | Workflow row exists in `running` with some tasks `completed` and one `running`. Process restart triggers the orphan reaper after `WISEORDER_ORPHAN_WORKFLOW_MAX_AGE_SECONDS` (default 600s) since the row's `created_at`. |
| expected | Reaped workflow marked `interrupted`; its `running` task also marked `interrupted`. Approval is NOT sent. Operator can decide to re-trigger. |
| residual risk | If reap age is too high (e.g., 1 hour) and an operator restarts often, real-but-slow workflows could be killed. Tune per environment. |
| inspect | `GET /workflows?status=interrupted` |
| response | Re-enqueue if intent stands. |

## F7 — Dashboard failure (FastAPI/uvicorn crash)

| | |
|---|---|
| current | The API task is a child asyncio task; a crash propagates. The orchestrator catches it at the top level via `gather(..., return_exceptions=True)` only on shutdown — during normal run, an API crash will surface as an unhandled exception in stderr. |
| expected | Workers continue processing. API serves nothing until restart. |
| residual risk | If uvicorn dies, the operator loses visibility but not work. Watchdog and queue still function. |
| inspect | `curl :8765/healthz` returns connection refused → restart |
| response | Restart the orchestrator. |

## F8 — Corrupted artifact (chain triple has wrong hash)

| | |
|---|---|
| current | The runtime layer does not own any chain. Chain integrity is the **wiseorder-protocol** layer's responsibility (`intellagent_runtime.chain.verify_chain`). This runtime never modifies chain artifacts. |
| expected | N/A here. See `wiseorder-protocol/intellagent_runtime/chain.py` and the protocol's `verify_chain` self-check. |
| residual risk | None at this layer. |
| inspect | `cd /Volumes/T7/2026-05-24 && bash verify.sh` |
| response | N/A. |

## F9 — Missing vector data (ChromaDB lost / disk wiped)

| | |
|---|---|
| current | `get_vector_store()` will recreate an empty collection at `data/chroma/`. Past commit summaries in Postgres are unaffected. New upserts will succeed. |
| expected | Search returns nothing until reindexed. Pipelines continue to function. |
| residual risk | Loss of search history. Rebuilding requires re-running the `save` step for past commits — no automatic backfill. |
| inspect | `GET /stats` → `vector_count` |
| response | Optional: re-run the pipeline for missed commits via `/trigger`. |

## F10 — Invalid verifier state (LLM returns non-JSON)

| | |
|---|---|
| current | `_parse_json` tries fenced extraction → regex extraction → falls back to returning the raw content as `summary`, empty `changed_files`, empty `changelog`, `risk_level="low"`. Logged as `summarizer_json_parse_failed`. |
| expected | Workflow continues with degraded summary; approval card still produced. No exception raised. |
| residual risk | Operator sees a low-quality summary. Risk classification defaults to `low`, which understates real high-risk changes. |
| inspect | `GET /workflows/{id}` — check `tasks[0].result.risk_level` vs expected |
| response | Re-trigger with the same SHA after fixing prompt or model; workflow is **not idempotent** in this path — duplicate suppression sees the existing failed/degraded run and skips. Manual fix: mark the old workflow `failed` in Postgres, then re-trigger. |

## F11 — Concurrent writes on the same workflow's `logs` field

| | |
|---|---|
| current | `_append_log` does read-modify-write of the JSONB `logs` array. With serial pipeline execution (one task at a time within a workflow), there is no concurrency. With multi-process workers and the same workflow_id being touched, last-write-wins → one log entry lost. |
| expected | Serial within workflow today. |
| residual risk | Real if you ever branch a workflow into parallel tasks. Today it cannot happen. |
| inspect | `GET /workflows/{id}` — log array length should match expected events |
| response | Do not introduce parallel branching without first migrating logs to a separate `workflow_events` table. |

## F12 — Orphaned jobs (job dequeued but worker died without fail-acknowledging)

| | |
|---|---|
| current | Same as F1. Job is gone from Redis. Workflow row reaped at next start. |
| expected | Operator triggers manual re-enqueue if needed. |
| residual risk | Lost work without operator action. |
| inspect | `GET /workflows?status=interrupted` |
| response | Same as F1. |

## F13 — Stale Redis lock (none used)

| | |
|---|---|
| current | **No locks.** The queue is RPUSH/BLPOP only; idempotency comes from Postgres-side `(repo, sha)` checks. |
| expected | No stale-lock risk because no locks. |
| residual risk | None — by design. |
| inspect | N/A |
| response | N/A |

## F14 — Runtime crash mid-workflow

| | |
|---|---|
| current | Process dies. On restart, the orphan reaper handles the workflow row (→ `interrupted`). The original job is gone from Redis (consumed by BLPOP before the crash). |
| expected | No re-execution. Operator decides. |
| residual risk | Manual re-trigger needed. |
| inspect | `GET /workflows?status=interrupted` |
| response | `POST /trigger` if you want to retry. |

## F15 — LLM provider outage / rate limit

| | |
|---|---|
| current | `_llm_call` catches `litellm.RateLimitError`, `APIConnectionError`, `ServiceUnavailableError` and retries up to `WISEORDER_LLM_MAX_RETRIES` (default 2) with exponential backoff. `asyncio.TimeoutError` after `WISEORDER_LLM_TIMEOUT_SECONDS` (default 60s) is also retried. |
| expected | Transient outages absorbed. Persistent outage → workflow fails, dead-letter. |
| residual risk | A worker is blocked for up to `timeout × (retries + 1) × backoff` ≈ ~6 minutes worst case. With 2 workers, two parallel failing workflows can stall the queue. |
| inspect | logs: `llm_timeout`, `llm_transient`; `GET /workflows?status=failed` |
| response | Verify provider status; consider switching `WISEORDER_LLM_MODEL` or pausing event watcher. |
