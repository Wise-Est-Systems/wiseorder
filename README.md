# wiseorder

[![tests](https://github.com/Wise-Est-Systems/wiseorder/actions/workflows/test.yml/badge.svg)](https://github.com/Wise-Est-Systems/wiseorder/actions/workflows/test.yml)
[![lint](https://github.com/Wise-Est-Systems/wiseorder/actions/workflows/lint.yml/badge.svg)](https://github.com/Wise-Est-Systems/wiseorder/actions/workflows/lint.yml)
[![migration-check](https://github.com/Wise-Est-Systems/wiseorder/actions/workflows/migration-check.yml/badge.svg)](https://github.com/Wise-Est-Systems/wiseorder/actions/workflows/migration-check.yml)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)

> Event-driven workflow orchestration that turns repository activity into reviewable, idempotent, auditable workflows with human approval gates.

---

## 1. Purpose

`wiseorder` is the operational layer of the WiseOrder stack. When a git commit lands in a watched repository, this runtime detects it, generates a structured engineering summary (LLM-backed), drafts a short social post about the change, persists everything to Postgres + ChromaDB, and produces an approval card on a local dashboard.

That is the entire scope. One workflow. No agent explosion. No autonomous loop. Every action that affects the outside world (sending an approval to Discord/Telegram) happens at a clearly named gate; no LLM output reaches a side effect without crossing it.

**This repo is NOT:**
- A governance kernel (that lives in [`wiseorder-protocol`](https://github.com/Wise-Est-Systems/wiseorder-protocol)).
- A multi-agent framework. There are no agent classes, no agent-to-agent messaging, no agent supervision tree.
- A general workflow engine. It runs exactly one pipeline (`commit_pipeline`); registering more is a 5-line change but not the point.
- A production-grade auth surface. Default bind is `127.0.0.1`; remote bind requires explicit opt-in and a token (see §12).

---

## 2. Architecture

Single Python process. Three external services: Postgres, Redis, embedded ChromaDB. No Kubernetes. No microservices. No queue broker beyond Redis lists.

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
                 ▼                      ▼                    │
            ┌─────────┐            ┌─────────┐         schedule
            │  Redis  │            │Postgres │       commit_pipeline
            │ (queue+ │            │ (state  │              │
            │  dedup) │            │  + audit│              │
            └─────────┘            └─────────┘              │
                 ▲                      ▲                   │
                 └──────── enqueue ─────┴───────────────────┘
                              ▲
                              │
                       commit detected
```

See [`docs/SYSTEM_MAP.md`](docs/SYSTEM_MAP.md) for the per-subsystem breakdown — what each owns, how each fails, how each is inspected.

---

## 3. Invariants

These are the lines a test or operator should be willing to assert. Documented in [`docs/RUNTIME_INVARIANTS.md`](docs/RUNTIME_INVARIANTS.md). Excerpt:

- **I3** — `(repo, sha)` is idempotent. Two events for the same commit produce one workflow row. Enforced atomically in Redis via `SET NX EX <ttl>`, not in the application layer.
- **I7** — Every LLM call has a timeout. `_llm_call` wraps `acompletion(...)` in `asyncio.wait_for(..., timeout=WISEORDER_LLM_TIMEOUT_SECONDS)`. A hung provider cannot block a worker indefinitely.
- **I12** — Orchestrator startup verifies Postgres and Redis or refuses to run. Workers are never started against unreachable services.
- **I14** — The runtime owns no chain, no spec, no canon. State this runtime owns: Postgres rows in 4 tables, Redis lists in 3 keys, ChromaDB collection `wiseorder`, file `logs/approvals.jsonl`. That is the complete list.
- **I17** — Refuses to bind to non-loopback hosts without an explicit `WISEORDER_API_ALLOW_REMOTE_BIND=true` AND a non-empty `WISEORDER_API_AUTH_TOKEN`. No silent exposure path.

---

## 4. Failure model

Documented in [`docs/FAILURE_MODEL.md`](docs/FAILURE_MODEL.md) as a table of 15 named failure modes, each with current behavior, expected behavior, residual risk, inspection command, and recommended response. Highlights:

| failure | current behavior | residual risk |
|---|---|---|
| Interrupted job (worker dies between BLPOP and completion) | Workflow row reaped to `interrupted` at next startup | No auto-retry by design |
| Redis restart | `redis.asyncio` auto-reconnects | Job popped mid-restart is lost |
| Postgres restart | Pool ping rejects stale connections; current step fails workflow | Half-committed workflows show `failed` |
| Duplicate event | Redis `SET NX` short-circuits; one workflow per SHA | Correct under multi-process workers |
| Malformed payload (missing `sha`/`repo`) | Raises `ValueError` before any DB write; dead-lettered | None |
| Mid-pipeline crash | Reaped to `interrupted` after `WISEORDER_ORPHAN_WORKFLOW_MAX_AGE_SECONDS` | Tune max-age to your restart cadence |
| LLM hang | Bounded by `WISEORDER_LLM_TIMEOUT_SECONDS` × `WISEORDER_LLM_MAX_RETRIES` | Worker blocked up to ~6 min worst case |

---

## 5. Quick start

```bash
git clone https://github.com/Wise-Est-Systems/wiseorder.git
cd wiseorder
make bootstrap          # python3.12 -m venv .venv + pip install -e ".[dev]"
make services-up        # docker compose up -d (Postgres on 5433, Redis on 6380)
cp .env.example .env    # then fill in ANTHROPIC_API_KEY and WISEORDER_WATCH_PATHS
make migrate            # alembic upgrade head
make run                # start orchestrator on http://127.0.0.1:8765
```

Make a commit in any watched repo. A workflow appears on the dashboard within ~1s; an approval card appears within ~10s (LLM-dependent).

---

## 6. Verification commands

```bash
make ci                 # the CI pre-flight: lint + test-pure
make test-pure          # smoke + hardening tests; no services required
make test               # full suite; service-dependent tests auto-skip when DB/Redis unreachable
make probe-services     # exits 0 iff DB + Redis both reachable
make lint               # ruff check + ruff format --check
```

Expected output of `make test-pure`:
```
21 passed, 8 skipped, 1 warning
```

The 8 skipped tests run when services are up; they exercise the Postgres-backed orphan reaper, the Redis SETNX dedup, the ChromaDB upsert path, and the dead-letter inspection endpoint.

---

## 7. CI status explanation

| workflow | what it gates | matrix |
|---|---|---|
| `tests` | unit + smoke + hardening tests + Alembic config parse | ubuntu / macos × py3.11 / py3.12 |
| `lint` | ruff check + ruff format check + mypy (lenient) | ubuntu × py3.12 |
| `migration-check` | `alembic upgrade head` from empty Postgres + idempotency + downgrade round-trip | ubuntu × py3.12 (Postgres service container) |

Every push to `main` triggers all three. Service-dependent tests inside `test_workflow.py` and `test_hardening_v2.py` auto-skip in `tests/` (no services in that workflow); `migration-check` exercises the Postgres path independently.

---

## 8. Recovery guarantees

Documented in [`docs/RECOVERY_MODEL.md`](docs/RECOVERY_MODEL.md). Survival matrix:

| failure                              | Redis jobs | Postgres rows | ChromaDB | In-flight workflow |
|---|---|---|---|---|
| Orchestrator SIGKILLed                | popped jobs lost; queued jobs survive | survive | survives | reaped → `interrupted` at next start |
| Redis container restarted             | survive (AOF on) | survive | survives | popped job lost → workflow reaped |
| Postgres container restarted          | survive | survive | survives | current step fails → workflow `failed` |
| ChromaDB data dir wiped               | survive | survive | empty, recreated | next save logs `vector_index_failed`; pipeline still completes |
| Mac power loss                        | ≤1s loss (Redis AOF default) | no loss (Postgres synchronous_commit) | small write-window risk | reaped → `interrupted` at next start |

**There is no auto-retry of failed workflows.** A failed workflow is the operator's signal to investigate. Once they decide, they re-trigger via `POST /trigger`.

---

## 9. Operational philosophy

- **Default deny.** API binds only to loopback unless two env vars explicitly say otherwise. LLM calls are time-bounded. Workflows are not retried automatically.
- **Idempotency at the domain key, not the job ID.** `(repo, sha)` is the dedup key. Job IDs are opaque and per-event; using them for dedup would miss duplicates from different watchers.
- **Inspectability over abstraction.** No ORM tricks. Every state transition is a SQL row update with a timestamp. Every workflow log entry has a UTC ISO-8601 `ts` field. `jq 'select(.workflow_id == 42)'` reconstructs the full life of a workflow from `logs/wiseorder.jsonl`.
- **Failure is sealed, not retried.** A failed job lands in the FAILED queue with a timestamped note and the original payload. No exponential-backoff retry loops; the operator gets the signal and decides.

---

## 10. Current limitations

- **Single-process by default.** Multi-process workers are correct (Redis SETNX handles cross-process dedup) but not exercised in CI.
- **Workflow logs are JSONB arrays with read-modify-write.** Concurrent appends on the same `workflow_id` would lose one (see `docs/FAILURE_MODEL.md` F11). The sequential pipeline does not hit this today; do not introduce parallel branching without migrating to a `workflow_events` table.
- **No CHANGELOG of LLM provider switches.** Switching `WISEORDER_LLM_MODEL` mid-flight is supported but not audited.
- **API GET endpoints are unauthenticated even when the bearer token is set.** Only mutating endpoints (`POST /trigger`, `POST /approvals/{id}/decide`) require the token. Acceptable on a private network; not appropriate on the open internet.
- **No license file yet.** `pyproject.toml` references one. Choosing a license is a deliberate decision the project owner has not yet made.

---

## 11. Roadmap

In approximate order of priority. Each is a single work order, not a phase.

1. Wire branch protection on GitHub (the policy in `docs/BRANCH_PROTECTION.md` is currently aspirational; the UI does not enforce it).
2. Add CI status badges that reference real workflow runs (already in this README; will activate once CI history exists).
3. Tighten ruff baseline; remove `continue-on-error: true` from `lint.yml`.
4. Migrate workflow logs from JSONB array to a `workflow_events` table (closes F11).
5. Add a dedicated `integration.yml` workflow that brings up Postgres + Redis as service containers and runs the full pipeline end-to-end.
6. Add `CHANGELOG.md` entries on every tagged release (template in `docs/RELEASE_PROCESS.md`).
7. Pick a license.

---

## 12. Security posture

Documented in [`docs/OPERATOR_GUIDE.md`](docs/OPERATOR_GUIDE.md). Summary:

| state | what happens at startup |
|---|---|
| `WISEORDER_API_HOST=127.0.0.1` (default) | starts normally; no auth required |
| `WISEORDER_API_HOST=0.0.0.0` and `WISEORDER_API_ALLOW_REMOTE_BIND=false` | **refuses to start** |
| Both env vars set, `WISEORDER_API_AUTH_TOKEN=""` | **refuses to start** |
| Both env vars set and token non-empty | starts with warning; mutating endpoints require `Authorization: Bearer <token>` |

The dashboard at `/` and the health/ready probes at `/healthz` / `/ready` remain open in all modes. Only `POST /trigger` and `POST /approvals/{id}/decide` are gated.

**Threat model in one sentence:** an attacker with network access to the bound interface should not be able to trigger workflows, approve approvals, or read state without the bearer token; the token is configured by the operator, transported via standard HTTP auth, and compared via Python `==` (not constant-time; acceptable on a private network, not appropriate for unauthenticated public exposure).

---

## 13. Release discipline

Documented in [`docs/RELEASE_PROCESS.md`](docs/RELEASE_PROCESS.md):

- Semver with explicit MAJOR/MINOR/PATCH rules.
- Pre-release checklist: clean tree → CI green → `make test` with services up → migration round-trip → `make probe-services`.
- Signed tags strongly preferred (`git tag -s`).
- Rollback path: `alembic downgrade <rev>` for schema, `git revert` for code.

No releases tagged yet — this repo is `v0.1.0-pre`. The first tag (`v0.1.0`) will follow once branch protection is configured on GitHub and a CHANGELOG entry is written for it.

---

## Further reading

- [`docs/SYSTEM_MAP.md`](docs/SYSTEM_MAP.md) — per-subsystem ownership and inspection
- [`docs/FAILURE_MODEL.md`](docs/FAILURE_MODEL.md) — 15 named failure modes
- [`docs/RUNTIME_INVARIANTS.md`](docs/RUNTIME_INVARIANTS.md) — what must always be true
- [`docs/RECOVERY_MODEL.md`](docs/RECOVERY_MODEL.md) — what survives what
- [`docs/OPERATOR_GUIDE.md`](docs/OPERATOR_GUIDE.md) — day-1 commands
- [`docs/BRANCH_PROTECTION.md`](docs/BRANCH_PROTECTION.md) — operational policy on `main`
- [`docs/RELEASE_PROCESS.md`](docs/RELEASE_PROCESS.md) — semver, checklist, rollback
- [`docs/REVIEWER_GUIDE.md`](docs/REVIEWER_GUIDE.md) — 30-minute path for an external reviewer
- [`docs/INTEGRATION.md`](docs/INTEGRATION.md) — how another system integrates
- [`CHANGELOG.md`](CHANGELOG.md) — release history
