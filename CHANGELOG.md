# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file documents the runtime layer at `~/Desktop/wiseorder` /
`github.com/Wise-Est-Systems/wiseorder`. The governance protocol layer
(`wiseorder-protocol`) has its own changelog.

## [Unreleased]

### Documentation
- Infrastructure-grade `README.md` with 13 sections (purpose, architecture,
  invariants, failure model, quick start, verification, CI, recovery,
  philosophy, limitations, roadmap, security, release).
- `docs/REVIEWER_GUIDE.md` — 30-minute external-reviewer path with brutal-honesty
  "what should concern you" closing.
- `docs/INTEGRATION.md` — how an external system integrates, trust boundaries,
  failure semantics.

### Stewardship
- CI badges in the README pointing at real workflow runs.
- Python version badge (3.11 | 3.12).

## [0.1.0-pre] — 2026-05-25

This is the pre-release that establishes the operational runtime as a
git-tracked, CI-tested, crash-recoverable system. No tagged release yet.

### Added
- **Operational orchestration core**: async orchestrator with worker pool,
  signal handling, graceful shutdown. Event-driven commit watcher.
- **Pipeline**: `workflows/commit_pipeline.py` — commit → summarize → draft →
  save → request approval.
- **Memory layer**: Postgres-backed `tasks` / `workflows` / `memory` /
  `approvals` tables; embedded persistent ChromaDB for vector search.
- **Queue**: Redis async queue with high/normal/failed lanes.
- **Approval gateway**: file (`logs/approvals.jsonl`) + Discord webhook +
  Telegram delivery. File is source of truth; webhooks are best-effort.
- **FastAPI dashboard** at `:8765` with pending approvals, dead-letter
  inspection, vector search, workflow logs.
- **LLM agents**: engineering summarizer (structured JSON output) and social
  drafter (≤280 chars), both via LiteLLM.
- **Redis SETNX `(repo, sha)` idempotency** replaces the v0.1 Postgres JSONB
  approach. Correct under multi-process workers. TTL = 3600s default.
- **Alembic migrations** with async env; `0001_initial_schema.py` matches
  the existing schema. `init_db()` runs `alembic upgrade head`.
- **API security**: orchestrator refuses to bind to non-loopback hosts
  without `WISEORDER_API_ALLOW_REMOTE_BIND=true` AND
  `WISEORDER_API_AUTH_TOKEN`. Mutating endpoints (`POST /trigger`,
  `POST /approvals/{id}/decide`) require `Authorization: Bearer <token>`
  when the token is set.
- **Startup orphan reaper**: `running` workflows older than
  `WISEORDER_ORPHAN_WORKFLOW_MAX_AGE_SECONDS` (default 600s) are flipped to
  `interrupted` at orchestrator start. Their running tasks too.
- **LLM call hardening**: every `acompletion(...)` is wrapped in
  `asyncio.wait_for(..., timeout=WISEORDER_LLM_TIMEOUT_SECONDS)` with
  bounded retries on transient errors. Provider hangs cannot block workers.
- **Correlation IDs**: `contextvars.ContextVar` for `workflow_id` and
  `job_id`; logging filter injects them as top-level JSON keys so an
  operator can grep `workflow_id == 42` across the entire log stream.
- **CI**: GitHub Actions workflows for tests (ubuntu/macos × py3.11/py3.12),
  lint (ruff + mypy lenient), migration-check (Postgres service container,
  upgrade + idempotent re-run + downgrade round trip).
- **Makefile** with `bootstrap`, `services-up`, `probe-services`, `migrate`,
  `test`, `test-pure`, `lint`, `format`, `run`, `ci`, `clean`.
- **Operator docs**: `SYSTEM_MAP.md`, `FAILURE_MODEL.md`,
  `RUNTIME_INVARIANTS.md`, `RECOVERY_MODEL.md`, `OPERATOR_GUIDE.md`,
  `BRANCH_PROTECTION.md`, `RELEASE_PROCESS.md`.
- **Tests**: 21 runnable + 8 service-dependent. Smoke (9), hardening v1 (10),
  hardening v2 (10).

### Fixed
- **ChromaDB upsert** (was `add`). Re-processing a commit could raise
  `IDAlreadyExists`, which was being silently caught and logged as a
  warning. Now uses `.upsert()` correctly.
- **Watcher coroutine race on shutdown**. `_GitHeadHandler.on_any_event`
  now checks `loop.is_closed()` before `asyncio.run_coroutine_threadsafe`
  and traps `RuntimeError` so a thread death cannot kill the process.
- **Watcher init-to-start gap**. Commits landing between handler
  construction and `observer.start()` were silently missed; now reconciled
  via `_reconcile_after_start()`.
- **Typed exceptions in `_read_commit`**. Was `except Exception`; is now
  typed `FileNotFoundError` / `CalledProcessError` with explicit log lines
  per failure mode.
- **Double diff truncation removed** (was truncating at 200k then 120k).
- **Settings + VectorStore singletons** now use `threading.Lock` for first
  init; the watcher thread and the main event loop both call into them.
- **Payload schema validation**: `_require_keys` raises `ValueError` before
  any DB write if `repo` or `sha` is missing.
- **Dead-letter notes** now include UTC ISO-8601 timestamp + attempt count.

### Changed
- `requires-python` lowered from `>=3.12` to `>=3.11`. CI matrix tests both
  versions; the code uses `from __future__ import annotations` everywhere
  and has no 3.12-specific features.
- `_append_log` race window documented as FAILURE_MODEL F11. Sequential
  pipeline does not hit it; multi-process parallel branching on the same
  workflow_id would. Migrating to `workflow_events` table is the eventual
  fix.

### Security
- API binds to `127.0.0.1` by default. Remote bind requires explicit
  opt-in AND a bearer token; orchestrator refuses to start otherwise.
- Bearer token comparison is direct equality (not constant-time);
  acceptable on a private network, not appropriate for unauthenticated
  public exposure. Documented in `docs/REVIEWER_GUIDE.md`.

### Pushed
- `github.com/Wise-Est-Systems/wiseorder` — private repo, every commit on
  `main` mirrored, every push triggers all three CI workflows.

### Verified
- All 21 runnable tests pass (8 service-dependent skip without Docker).
- Alembic `upgrade head` succeeds from empty Postgres in CI; `downgrade
  base → upgrade head` round-trips cleanly.
- CI matrix all-green on the latest commit: ubuntu/macos × py3.11/py3.12,
  plus lint, plus migration-check.
