# Security policy

## Reporting a vulnerability

Do **not** open a public issue for anything that could bypass the human approval gate, expose committed-but-unapproved drafts, or leak operator credentials. Use one of:

- **GitHub Security Advisories** — preferred. Open a private advisory at <https://github.com/Wise-Est-Systems/wiseorder/security/advisories/new>.
- **Email** — `security@truth.systems` (PGP-encrypted preferred; fingerprint published in release notes per major version).

Include enough information to reproduce: version or commit SHA, platform, configuration relevant to the issue (bind address, services attached, LLM provider), a reproducer, and your impact assessment.

We will:

1. Acknowledge receipt within **two business days**.
2. Assign a severity using CVSS v3.1 and a triage owner within **five business days**.
3. Provide a remediation plan, expected fix window, and disclosure timeline.

## Disclosure window

Default coordinated-disclosure window is **90 days** from the initial report. We will request an extension if (and only if) a fix demands more time, and we will communicate the new date in writing. Reporters may publish at the end of the agreed window.

## Scope

In scope:

- The orchestrator (`core/orchestrator/`) — including any way to dispatch a workflow that bypasses the registered pipeline registry.
- The approval gate (`core/approvals/`) — including any way to commit an outbound side effect (Discord/Telegram/external API) without an approval card being created and approved.
- The workflow registry and the `commit_pipeline` — including any way to register, replace, or invoke a workflow without going through the orchestrator's `register()`.
- The FastAPI surface (`api/`) — including any unauthenticated endpoint that mutates state, any path-traversal vector in the dashboard, any way to read approval-card content without the configured token when remote bind is enabled.
- The watcher (`core/watchers/`) — including any way to trigger pipeline execution from a path outside the configured watch list.
- The queue surface (`core/queues/`) — including any deduplication bypass that would re-run an idempotent job, any Redis-level injection via job payload.
- The database layer (`core/memory/`) — including any way to insert, modify, or delete a committed workflow row outside the documented migration path, any SQL injection via user-supplied job payload.
- The settings loader (`configs/settings.py`) — including any way to override a security-relevant setting (bind address, auth token, allowed origins) without the documented env-var prefix.

Out of scope:

- Issues that require an already-compromised operator account or already-compromised Postgres/Redis credentials.
- Misuse of the local FastAPI server bound to `127.0.0.1` by a process running on the same host (the loopback bind is a trust boundary delegated to the operator's OS).
- Third-party services consumed by this runtime: Postgres, Redis, ChromaDB, the LLM provider, Discord / Telegram webhooks. Report vulnerabilities in those services to their respective maintainers.
- Issues that require the operator to disable the approval gate via documented configuration (the configuration itself is the warning).

## Threat model

The runtime MUST guarantee:

| Property                                                                       | Mechanism |
|--------------------------------------------------------------------------------|-----------|
| No outbound side effect occurs without explicit human approval                 | All channel adapters route through `core/approvals/gateway.py`; the dashboard surfaces the approval card; only an approved card may be submitted. |
| The default bind is loopback only                                              | `configs/settings.py` defaults `bind=127.0.0.1`; remote bind requires `WISEORDER_ALLOW_REMOTE=true` AND a non-empty `WISEORDER_AUTH_TOKEN`. |
| Approval tokens are compared in constant time                                  | `hmac.compare_digest` on every token check; never `==`. |
| LLM-generated drafts cannot reach the outside world without an approval gate   | Drafters write to the approval queue; submission is gated. |
| Watcher scope is bounded to the configured paths                               | The watcher rejects events from any path not in the explicit allow-list; symlinks are resolved before allow-list comparison. |
| Workflow execution is idempotent on `(repo, sha)` for the commit pipeline      | Dedup acquire/release via Redis with timeout; double-claim returns the existing workflow ID. |
| Failed jobs are routed to a dead-letter queue, not silently dropped            | Dead-letter inspection is part of the audit surface. |

The runtime does **not** defend against:

- A malicious LLM that crafts drafts intended to manipulate the approver (the approver is the trust anchor; this is by design).
- An attacker with shell access to the host running the runtime.
- An attacker who controls the Postgres or Redis instances the runtime depends on.
- An attacker who controls the operator's Discord or Telegram account (those channels are how approval cards are surfaced; compromise of either is a compromise of the approval gate).
- Resource-exhaustion attacks via maliciously large commit payloads (operator-side rate limiting is the operator's responsibility).
- Supply-chain attacks on the runtime's own dependencies.

## Approval-gate integrity is non-negotiable

The approval gate is the trust boundary between LLM-generated content and the outside world. Any code path that submits an outbound side effect without an approved card is a P0 vulnerability under this policy, regardless of whether the content was generated by an LLM or hand-written into a workflow.

## Configuration-as-attack-surface

The following settings are security-relevant. Changes to defaults must be reviewed:

- `WISEORDER_BIND` — `127.0.0.1` by default; only widen with explicit consent.
- `WISEORDER_ALLOW_REMOTE` — must be `false` by default.
- `WISEORDER_AUTH_TOKEN` — required when remote bind is enabled; empty token = refuse to start.
- `WISEORDER_DATABASE_URL` — should not be logged in any error path.
- `WISEORDER_LLM_PROVIDER_KEY` — never logged; redacted from any traceback.

A regression that changes a documented secure default to a less-secure one is a vulnerability under this policy.

## External dependencies

The runtime depends on Postgres, Redis, ChromaDB, and one or more LLM providers via `litellm`. Vulnerabilities in those projects are out of scope for this policy but in scope for operational deployment: the operator is responsible for keeping them patched.
