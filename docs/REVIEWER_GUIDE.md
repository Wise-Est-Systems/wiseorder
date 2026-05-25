# REVIEWER_GUIDE

A 30-minute path for an external engineer to validate everything this repo claims. No prior context. Plain commands. Real outputs.

## Prerequisites

- Python 3.11 or 3.12
- Docker Desktop (or any Docker daemon)
- ~600 MB disk for `.venv` (ChromaDB pulls onnxruntime; this is the largest dep)

## Minute 0–5 — Clone + bootstrap

```bash
git clone https://github.com/Wise-Est-Systems/wiseorder.git
cd wiseorder
make bootstrap
```

Expected: a `.venv/` created, package installed editable. No errors.

## Minute 5–10 — Pure tests (no services)

```bash
make test-pure
```

Expected:
```
21 passed, 8 skipped, 1 warning in ~2s
```

The 8 skipped tests need Postgres + Redis; they exercise idempotency, the orphan reaper, ChromaDB upsert, and the dead-letter inspection endpoint. We run them in the next step.

## Minute 10–15 — Services up + full sweep

```bash
make services-up           # docker compose up -d (Postgres on 5433, Redis on 6380)
make migrate               # alembic upgrade head
make test                  # full suite
```

Expected:
```
29 passed, 1 warning in ~3s
```

Every test passes. No skips when services are reachable.

## Minute 15–20 — CI verification (on GitHub)

Open https://github.com/Wise-Est-Systems/wiseorder/actions and confirm:

- Most recent `tests` run is green on **all four** matrix combinations (ubuntu/macos × py3.11/py3.12).
- Most recent `lint` run is green.
- Most recent `migration-check` run is green — proves Alembic upgrades cleanly from an empty Postgres and round-trips through `downgrade base → upgrade head`.

## Minute 20–25 — Crash recovery + idempotency proofs

The runtime's integrity claims live in two test files:

```bash
.venv/bin/pytest tests/test_hardening.py::test_idempotency_same_sha_skips -v
.venv/bin/pytest tests/test_hardening.py::test_orphan_reaper_marks_stale_running -v
.venv/bin/pytest tests/test_hardening.py::test_vector_upsert_replaces_existing_id -v
.venv/bin/pytest tests/test_hardening.py::test_dead_letter_inspection_returns_failed_job -v
.venv/bin/pytest tests/test_hardening_v2.py::test_dedup_acquire_blocks_second_claim -v
```

Each prints `PASSED`. These are the operational integrity proofs:
- **Idempotency**: two events for the same `(repo, sha)` produce one workflow row.
- **Orphan reaper**: a workflow stuck `running` past max-age is flipped to `interrupted` at startup.
- **ChromaDB upsert**: re-processing a commit does not raise `IDAlreadyExists`.
- **Dead-letter**: failed jobs appear in `GET /queues/failed` with their error notes.
- **Redis SETNX**: concurrent dedup claims are atomic; second claim sees `PENDING`.

## Minute 25–28 — Inspect what the system actually owns

```bash
ls core/memory/           # 4 SQL tables: tasks, workflows, memory, approvals
ls core/queues/           # Redis lists: high/normal/failed + dedup keys
ls alembic/versions/      # one migration: 0001_initial_schema.py
ls workflows/             # one workflow: commit_pipeline.py
ls agents/                # two LLM agents: engineering summarizer + social drafter
```

State this runtime owns: **exactly** four Postgres tables, three Redis lists, one ChromaDB collection, one JSONL file. That's the entire ownership surface. See `docs/RUNTIME_INVARIANTS.md` I14.

## Minute 28–30 — Inspect failure model + invariants

Skim three docs in order:
1. [`docs/RUNTIME_INVARIANTS.md`](RUNTIME_INVARIANTS.md) — 18 named invariants. Each is grep-greppable from code.
2. [`docs/FAILURE_MODEL.md`](FAILURE_MODEL.md) — 15 failure modes. Current behavior, expected behavior, recovery, inspection command.
3. [`docs/RECOVERY_MODEL.md`](RECOVERY_MODEL.md) — survival matrix: what survives Redis restart, Postgres restart, Mac power loss, ChromaDB wipe.

## What should concern you

Brutal honesty. The things that would slow my approval if I were reviewing this for production adoption today:

1. **No tagged release.** This is `v0.1.0-pre`. Nothing is signed. Operating against `main` is the only path.
2. **Lint and mypy run `continue-on-error: true`.** The CI reports drift; it does not block on it. A reviewer should expect the baseline to be cleaned up before "lint" becomes meaningful.
3. **Branch protection is documented, not enforced.** GitHub UI side is not configured. Force-push to `main` is technically allowed.
4. **No CHANGELOG entries for individual fixes.** `git log --oneline` is the only release history. CHANGELOG.md exists with template-level entries; granular per-commit attribution lives in commit messages.
5. **GET endpoints are not auth-gated even with bearer token set.** Reading state is intentionally open; writing is gated. Acceptable on a private network; **not** appropriate for unauthenticated public exposure.
6. **No `integration.yml` workflow.** The full pipeline (commit → summarize → draft → save → approve) is exercised only locally; CI runs unit + smoke + hardening. The integration path lives in `test_workflow.py` and requires services.
7. **Single-process by default.** Multi-process is correct (Redis SETNX is atomic) but not exercised by CI; an adopter scaling horizontally should treat that as their own validation gate.
8. **LLM token-comparison is direct equality (not constant-time).** Acceptable on a private network; not appropriate for an open auth surface.
9. **The pipeline's LLM provider list is implicitly Anthropic.** Switching to a different LiteLLM-compatible model is one env var; the prompt templates were tuned against Claude Sonnet 4.6 and have not been re-tuned for others.
10. **No license file.** `pyproject.toml` says "see LICENSE" — there isn't one. By default copyright applies; not yet open-source.

If any of those would block you, raise them and the project owner can decide whether to action or accept. None of them are integrity bugs; all of them are real adoption-readiness gaps.
