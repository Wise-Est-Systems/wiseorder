# INTEGRATION

How an external system integrates with `wiseorder`. What it can rely on, what it must not assume.

## What this runtime exposes

A FastAPI HTTP server on `127.0.0.1:8765` (configurable). No other surface — no library API for embedding, no Python package designed for downstream `import`. Integration is HTTP.

### Stable endpoints (read)

These are the surface a dashboard, monitoring system, or external observer can rely on. Backwards compatibility is honored within a MINOR version.

| method | path | returns |
|---|---|---|
| GET | `/health`, `/healthz` | queue depths + Redis ping; always 200 |
| GET | `/ready` | 200 iff Postgres + Redis reachable; 503 otherwise |
| GET | `/stats` | task / workflow / queue counts + vector size |
| GET | `/tasks?status=&limit=` | recent tasks |
| GET | `/workflows?status=&limit=` | recent workflows |
| GET | `/workflows/{id}` | one workflow plus its tasks |
| GET | `/approvals?pending=&limit=` | pending or all approvals |
| GET | `/queues/failed?limit=` | dead-letter inspection (failed jobs + notes) |
| GET | `/memory/recent?limit=` | recent commit summaries |
| GET | `/memory/search?q=&n=` | vector search (ChromaDB) |
| GET | `/logs/recent?limit=` | recent workflow log entries (sorted by `ts` descending) |

### Stable endpoints (write — auth-gated when token set)

| method | path | body | effect |
|---|---|---|---|
| POST | `/trigger` | `{"type": "<job_type>", "payload": {...}}` | enqueue a job to the HIGH queue |
| POST | `/approvals/{id}/decide` | `{"decision": "approved" \| "rejected"}` | record an approval decision |

When `WISEORDER_API_AUTH_TOKEN` is set, both require `Authorization: Bearer <token>`. GET endpoints remain open.

## Trust boundaries

| boundary | who controls it | what crosses it |
|---|---|---|
| The bind address | The operator via `WISEORDER_API_HOST` | All HTTP traffic. Default 127.0.0.1 = same-host only. Non-loopback bind requires explicit opt-in AND an auth token. |
| The bearer token | The operator via `WISEORDER_API_AUTH_TOKEN` | Authorization for mutating endpoints. Token is compared via direct equality (acceptable on private networks; not constant-time). |
| The LLM provider | `WISEORDER_LLM_MODEL` + provider env keys | Engineering summary + social post are LLM-generated. The LLM call is bounded by timeout + retries; output is parsed as JSON or falls back to a degraded summary. |
| The webhook destinations | `WISEORDER_DISCORD_WEBHOOK_URL` + Telegram vars | Approval cards delivered out-of-band. Best-effort; the file `logs/approvals.jsonl` is the source of truth. |
| The watched repos | `WISEORDER_WATCH_PATHS` | The runtime reads `git log`, `git show`, `git rev-parse` on these paths only. No writes. |

## Failure semantics an integrator must accept

- **Idempotency is on `(repo, sha)`.** If you submit the same commit twice via `POST /trigger`, the second call returns `{"job_id": "..."}` (enqueued) — but the pipeline itself short-circuits via Redis SETNX and returns the existing workflow_id. You will not see two approval cards for the same commit.
- **Workflows can land in `interrupted`.** If the orchestrator restarts mid-pipeline, the workflow row sits in `running` until the next startup's reaper flips it to `interrupted`. An integrator polling `GET /workflows?status=interrupted` should expect this and decide whether to re-trigger.
- **LLM output is non-deterministic.** Two runs of the same commit (after TTL or via `release()`) may produce different summaries. The risk level (`low` / `medium` / `high`) is regenerated each time. Do not rely on byte-equality.
- **The vector store is regenerable but not auto-backfilled.** Wiping `data/chroma/` does not corrupt Postgres; it just empties the search index. A re-trigger of past commits is the operator's call.
- **Webhook delivery is not retried.** A Discord webhook failure is logged and the approval row's `delivered_via` field reflects what succeeded (`"file"` only, or `"file,discord"`, etc.). The integrator can poll `GET /approvals` and decide whether to retry the webhook themselves.

## Reproducibility expectations

- **Service ports**: Postgres on `5433`, Redis on `6380` (deliberately non-default to avoid collision with other services). Changing them is a `docker-compose.yml` edit; nothing in the runtime hardcodes them.
- **Schema migrations**: Alembic is the source of truth. `alembic upgrade head` is idempotent. `alembic downgrade base` is exercised in CI and works.
- **Test-determinism**: `pytest tests/test_smoke.py tests/test_hardening.py tests/test_hardening_v2.py` is fully deterministic. Service-dependent tests are deterministic given service availability.
- **CI-determinism**: GitHub Actions matrix runs the same commit on ubuntu/macos × py3.11/py3.12. All four must pass.

## Operational assumptions

The runtime assumes:
- Postgres ≥ 14 (uses JSONB containment, server-side timestamp defaults).
- Redis ≥ 6 (uses `SET key value NX EX ttl` semantics).
- One process per database. No coordination between multiple orchestrator instances is implemented.
- The watcher's file-system events arrive within ~100 ms of the `git commit`. Filesystems with delayed inotify (network mounts, some FUSE drivers) may miss commits; the watcher does not poll.
- `git` is on PATH and operable on the watched repos. A missing `git` binary surfaces as `git_missing_from_path` in logs, and watched repos contribute empty diffs.

## What this runtime does NOT provide for integration

- No webhook subscription model. To get events out of the runtime, poll `/workflows`, `/approvals`, or `/logs/recent`.
- No RPC layer. HTTP only.
- No SDK. The HTTP contract is the SDK.
- No multi-tenancy. One Postgres database = one runtime's worth of state.
- No write-side authentication beyond a single bearer token. No OAuth, no per-user gates.
- No rate limiting. An attacker with the token can flood `/trigger`.

If your integration needs any of those, build them on top — the HTTP surface is stable enough to wrap.
