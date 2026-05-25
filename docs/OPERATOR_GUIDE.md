# OPERATOR_GUIDE

Day-one commands. No fluff.

## First run

```
cd ~/Desktop/wiseorder
docker compose up -d                                # Postgres, Redis
cp .env.example .env                                # fill in ANTHROPIC_API_KEY + WISEORDER_WATCH_PATHS
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m core.orchestrator.main --probe-services   # verify reachable
python -m core.orchestrator.main                    # start
open http://127.0.0.1:8765
```

Make a git commit in any watched repo. Within ~1s a workflow appears on the dashboard. Within ~10s (LLM-dependent) an approval card appears in PENDING APPROVALS.

## Common operations

### Check health
```
curl -s :8765/healthz | jq               # always 200; shows queue depths
curl -s :8765/ready                       # 200 iff DB + Redis both reachable; 503 otherwise
```

### Look at recent activity
```
curl -s :8765/stats | jq
curl -s ':8765/workflows?limit=10' | jq '.[] | {id, status, created_at}'
```

### Inspect one workflow
```
curl -s :8765/workflows/42 | jq
```

### See failed jobs (dead-letter)
```
curl -s :8765/queues/failed | jq
```

### Decide on a pending approval
```
curl -X POST :8765/approvals/7/decide -H 'content-type: application/json' \
     -d '{"decision":"approved"}'
```

### Manually trigger a commit pipeline
```
curl -X POST :8765/trigger -H 'content-type: application/json' \
     -d '{"type":"commit_pipeline","payload":{
           "repo":"/Users/thekingflame/Desktop/wiseorder-protocol",
           "sha":"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
           "subject":"manual probe",
           "author":"op",
           "diff":""}}'
```

### Search memory by meaning
```
curl -s ':8765/memory/search?q=chain%20triple' | jq
```

### Read the audit log
```
tail -f logs/wiseorder.jsonl | jq 'select(.workflow_id == 42)'
```

### Read all approvals (file is source of truth)
```
tail -f logs/approvals.jsonl
```

## Stopping cleanly

```
# in the orchestrator's terminal: Ctrl-C  (SIGINT)
# or, from another terminal:
pkill -INT -f 'core.orchestrator.main'
```

Workers finish their current BLPOP (≤1s) then exit. Watcher and API close gracefully. In-flight workflows remain `running` in Postgres and get reaped to `interrupted` at next start.

## Stopping the services

```
docker compose down                       # stop containers; data volumes survive
docker compose down -v                    # WIPE data — Postgres + Redis lose everything
```

## Security posture

The API binds to `127.0.0.1` by default. **Anything else requires explicit opt-in.**

| state | what happens at startup |
|---|---|
| `WISEORDER_API_HOST=127.0.0.1` (default) | starts normally; no auth required |
| `WISEORDER_API_HOST=0.0.0.0` and `WISEORDER_API_ALLOW_REMOTE_BIND=false` | **REFUSES to start.** Loud error. |
| `WISEORDER_API_HOST=0.0.0.0`, `WISEORDER_API_ALLOW_REMOTE_BIND=true`, `WISEORDER_API_AUTH_TOKEN=""` | **REFUSES to start.** Won't expose unauthenticated mutations. |
| `WISEORDER_API_HOST=0.0.0.0`, both vars set | starts with warning; `POST /trigger` and `POST /approvals/{id}/decide` require `Authorization: Bearer <token>` |

When the auth token is set, mutating endpoints check the header:
```
curl -X POST :8765/trigger -H "Authorization: Bearer YOUR_TOKEN" -H 'content-type: application/json' -d '...'
```
GET endpoints remain open (they don't change state). The dashboard works at localhost without a token regardless.

Generate a token:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Tuning knobs (env vars, all `WISEORDER_*` prefixed)

| var | default | meaning |
|---|---|---|
| `WISEORDER_LLM_MODEL` | `claude-sonnet-4-6` | LiteLLM model string. Switch provider by switching model. |
| `WISEORDER_LLM_TIMEOUT_SECONDS` | 60.0 | Hard cap on each LLM call. |
| `WISEORDER_LLM_MAX_RETRIES` | 2 | Retry budget on transient LLM failures. |
| `WISEORDER_ORPHAN_WORKFLOW_MAX_AGE_SECONDS` | 600 | A `running` workflow older than this at startup is `interrupted`. |
| `WISEORDER_DEDUP_TTL_SECONDS` | 3600 | Redis SETNX claim TTL on (repo, sha). |
| `WISEORDER_WATCH_PATHS` | `""` | Comma-separated absolute paths to repos to watch. |
| `WISEORDER_API_HOST` | `127.0.0.1` | Bind only to localhost by default. |
| `WISEORDER_API_PORT` | `8765` | |
| `WISEORDER_API_ALLOW_REMOTE_BIND` | `false` | Required for non-loopback binds. |
| `WISEORDER_API_AUTH_TOKEN` | `""` | Bearer token; required when remote-binding. |
| `WISEORDER_DISCORD_WEBHOOK_URL` | `""` | Optional approval delivery. |
| `WISEORDER_TELEGRAM_BOT_TOKEN` + `_CHAT_ID` | `""` | Optional approval delivery. |
| `WISEORDER_LOG_LEVEL` | `INFO` | Standard Python log levels. |

## Diagnosing common situations

### "no commits being detected"
```
ls -la $WISEORDER_WATCH_PATHS/.git           # is it actually a repo?
grep event_watcher_watching logs/wiseorder.jsonl
grep watcher_skip logs/wiseorder.jsonl
```
The watcher logs each repo it picks up at startup. If it logs `event_watcher_skip_non_git`, the path is wrong.

### "workflow stuck in running"
```
curl -s :8765/workflows/<id> | jq '.logs'
```
Check the last log entry. If the orchestrator crashed, restart it — the orphan reaper will mark it `interrupted` at the next boot (if past `WISEORDER_ORPHAN_WORKFLOW_MAX_AGE_SECONDS`).

### "LLM keeps timing out"
```
grep llm_timeout logs/wiseorder.jsonl | tail -5
```
Either your network is bad, the provider is down, or your timeout is too tight. Raise `WISEORDER_LLM_TIMEOUT_SECONDS`.

### "approvals not reaching Discord"
```
grep discord_delivery logs/wiseorder.jsonl | tail -5
```
File row in `logs/approvals.jsonl` is always written. Webhook is best-effort. Check the URL is current.

### "duplicate work / duplicate approvals"
Should not happen — idempotency on `(repo, sha)`. If it does:
```
curl -s ':8765/workflows?limit=50' | jq '.[] | {id, logs: (.logs[0].data)}' | grep <sha>
```
If you see two workflows for the same SHA, file a bug — the idempotency JSONB query failed.

## Running tests

```
pytest tests/test_smoke.py -v             # 9 tests, no services required
pytest tests/test_hardening.py -v         # 6 pure + 4 service-dependent (auto-skip)
pytest tests/ -v                          # everything
```
