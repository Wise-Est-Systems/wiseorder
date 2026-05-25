# RUNTIME_INVARIANTS

Things that must always hold. If any of these become false, the system is broken — these are the lines a test or an operator should be willing to assert.

## I1 — A workflow row is created before any LLM call

`run_commit_pipeline` flushes the `Workflow` row before invoking the summarizer. This guarantees every external (paid) call is preceded by a Postgres row that lets the operator find the work.

## I2 — A workflow ends in exactly one of: `running`, `completed`, `failed`, `interrupted`

No other statuses exist. The orphan reaper transitions `running → interrupted`. The pipeline transitions `running → {completed, failed}`. There is no other transition.

## I3 — `(repo, sha)` is idempotent within one orchestrator process

Two calls to `run_commit_pipeline` with the same `(repo, sha)` produce exactly one new workflow row. The second call returns `{"skipped": True, "workflow_id": <first>}`.

## I4 — A failed workflow does not silently retry

Workers do not retry. A failed job moves to the FAILED queue with the error in `notes` and stays there for inspection. The operator decides whether to re-trigger.

## I5 — Approval delivery is best-effort; file persistence is mandatory

The file write to `logs/approvals.jsonl` happens before any webhook delivery is attempted. If both webhooks fail, the file is still the source of truth. The approval row in Postgres is the canonical record.

## I6 — Chain artifacts are out of scope

This runtime does not create, modify, or verify `.win` triples. That responsibility belongs to `wiseorder-protocol/intellagent_runtime`. If you ever find code in this repo that writes to a `chain/` directory or computes `consequence_proof`, it is a layering violation and must be removed.

## I7 — Every LLM call has a timeout

`_llm_call` wraps every `acompletion(...)` in `asyncio.wait_for(..., timeout=...)`. A hung provider cannot block a worker indefinitely. If you add a new agent, **it must call `_llm_call`**, not `acompletion` directly.

## I8 — Every workflow log entry is timestamped in UTC, ISO-8601

`_log_entry` uses `datetime.now(timezone.utc).isoformat()`. No local time, no naive datetimes, no `time.time()` floats. Operators reading `logs` JSONB should be able to grep by timestamp prefix.

## I9 — Vector writes use `upsert`, never `add`

Re-processing a commit (after a retry or duplicate event that slipped through idempotency) must not raise. If you ever see `IDAlreadyExists` in logs, the runtime is calling `.add` somewhere — fix it.

## I10 — Singletons are constructed under a lock

`get_settings()` and `get_vector_store()` both use a `threading.Lock` for first-time construction. The event-watcher thread and the main event loop both call into these helpers; without the lock, two instances could be built and one orphaned.

## I11 — Correlation IDs flow through every workflow log line

`run_commit_pipeline` calls `bind_workflow(workflow_id)` at entry. While inside the binding, every `log.info(...)` call in this process emits a record with `workflow_id` as a top-level JSON key. Operators can `jq 'select(.workflow_id == 42)'` to reconstruct the full life of a workflow.

## I12 — Orchestrator startup verifies both services or refuses to run

`Orchestrator.start()` raises `RuntimeError` if `db_ping()` or `redis ping` fail. Workers are never started against unreachable services. Use `--probe-services` for a non-destructive check.

## I13 — Watchdog observer never schedules a coroutine onto a closed loop

`_GitHeadHandler.on_any_event` checks `loop is None or loop.is_closed()` before calling `run_coroutine_threadsafe`. RuntimeErrors from the schedule call are caught and logged. The Watchdog thread cannot kill the process via an unhandled exception.

## I14 — The runtime owns no chain, no spec, no canon

State this runtime owns: Postgres rows in 4 tables, Redis lists in 3 keys, ChromaDB collection `wiseorder`, file `logs/approvals.jsonl`. **That is the complete list.** Anything else (governance specs, chain triples, conformance vectors) belongs to a different layer.
