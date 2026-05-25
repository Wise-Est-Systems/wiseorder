# RECOVERY_MODEL

What survives what, and what the operator must do.

## Survival matrix

| failure                              | Redis jobs | Postgres rows | ChromaDB | Approval file | In-flight workflow |
|---|---|---|---|---|---|
| Orchestrator process killed (SIGKILL) | popped jobs lost; queued jobs survive | survive | survives | survives | reaped → `interrupted` at next start |
| Redis container restarted             | survive if AOF on (default in our compose) | survive | survives | survives | currently-popped job lost → workflow reaped |
| Postgres container restarted          | survive | survive | survives | survives | current workflow step fails → workflow `failed` |
| ChromaDB data dir wiped               | survive | survive | empty, recreated | survives | next save logs `vector_index_failed`; pipeline still completes |
| Mac power loss                        | depends on Redis AOF fsync; default = "everysec" → ≤1s loss | depends on Postgres `synchronous_commit`; default = on → no loss | small risk during write | small risk during append | reaped → `interrupted` at next start |
| `logs/approvals.jsonl` deleted        | unaffected | unaffected | unaffected | starts fresh | unaffected |
| `data/chroma/` deleted                | unaffected | unaffected | rebuilt empty | unaffected | next save degrades silently |

## Boot sequence (what runs and in what order)

1. **Config load** — `get_settings()` reads `.env`, validates with Pydantic.
2. **Logging configured** — `configure_logging()` opens stdout + `logs/wiseorder.jsonl`, installs correlation filter.
3. **DB ping + `init_db()`** — `CREATE TABLE IF NOT EXISTS` for all four tables. Fails closed on unreachable Postgres.
4. **Redis ping** — fails closed on unreachable Redis.
5. **Orphan reaper** — any workflow in `running` older than `WISEORDER_ORPHAN_WORKFLOW_MAX_AGE_SECONDS` (default 600s) flipped to `interrupted`; its running tasks flipped to `interrupted` with `error="orphaned at orchestrator startup"`.
6. **Workers started** — `N` async workers (default 2) begin draining HIGH then NORMAL queues.
7. **Watcher started** — Watchdog observers for each `WISEORDER_WATCH_PATHS` entry; after `observer.start()`, `_reconcile_after_start()` re-reads HEAD to close the init-to-start gap.
8. **API started** — uvicorn on `WISEORDER_API_HOST:WISEORDER_API_PORT` (default `127.0.0.1:8765`).

If any step fails, no later step runs. Errors are logged with a clear `hint` field (e.g. `"run 'docker compose up -d' then retry"`).

## Shutdown sequence (SIGINT / SIGTERM)

1. **Stop signal received** — `loop.add_signal_handler(SIGINT, stop_event.set)` (already wired).
2. **`_stop` event set** — workers exit their BLPOP loop within ≤1s (BLPOP timeout).
3. **Watcher stopped** — observer thread joined with 5s timeout.
4. **API server told to exit** — `uvicorn.Server.should_exit = True`; API task awaited up to 5s.
5. **Workers awaited** — `gather(*self._workers, return_exceptions=True)`.
6. **Redis client closed**.

Workflows in-flight at SIGTERM remain `running` in Postgres until the next start, when the reaper marks them `interrupted`.

## Recovery actions (operator)

### After a planned restart

```
docker compose up -d                                # services back
python -m core.orchestrator.main --probe-services   # confirm reachable
python -m core.orchestrator.main                    # start
```

The orphan reaper logs how many workflows it touched at boot. Look for `orphan_workflows_reaped` in `logs/wiseorder.jsonl`.

### After a crash with `failed` workflows

```
curl :8765/workflows?status=failed | jq '.[] | {id, completed_at}'
curl :8765/workflows/<id> | jq '.logs[-3:]'         # what went wrong
```

If the failure is transient (LLM 5xx, network blip), re-enqueue:

```
curl -X POST :8765/trigger -H 'content-type: application/json' \
  -d '{"type":"commit_pipeline","payload":{"repo":"...","sha":"...","subject":"...","author":"...","diff":""}}'
```

The pipeline is idempotent on `(repo, sha)` ONLY for `running` and `completed` workflows. **A previous `failed` workflow does NOT block a new run.** This is deliberate: a failed workflow is the operator's signal to investigate; once they've decided to re-trigger, they want a fresh attempt.

### After a malformed-job dead-letter

```
curl :8765/queues/failed | jq '.[] | {id, type, notes}'
```

Dead-letter jobs are not auto-replayed. Inspect, decide, optionally `POST /trigger` a corrected version. There is no built-in command to drain the failed queue — it grows until cleared manually with `redis-cli LTRIM wiseorder:queue:failed_jobs 0 -1`.

### After Postgres data loss (catastrophic)

There is no application-level backup. Take a `pg_dump` cron job if you care:

```
docker exec wiseorder-postgres pg_dump -U wiseorder wiseorder > backup.sql
```

The chain artifacts (`.win` triples) are in `wiseorder-protocol`, not this runtime. They are not affected by this runtime's data loss.

## What is NOT recovered automatically

- Lost popped jobs (mid-flight at the time of the crash).
- Dead-letter queue contents (manual disposition required).
- Approval webhooks that failed to deliver (file row exists; resend by hand if needed).
- Vector data after `data/chroma/` deletion (rebuild via manual triggers).
