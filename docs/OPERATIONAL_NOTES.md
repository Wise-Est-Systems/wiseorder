# OPERATIONAL_NOTES

Honest observations from operating STACK_001 in the operationalization session of 2026-05-25. The work order said "operate for multiple days" — that's not possible inside one session. What follows is what I actually observed in this session, not what I'd expect days of operation to reveal.

## Session-time constraints (the elephant in the room)

The Phase 1 step "ensure Docker Desktop running" failed: **Docker Desktop is not actually installed on this Mac.** Only an empty `/Applications/Docker.app` directory stub exists. The `docker` CLI is on PATH (probably from a prior Homebrew install or partial uninstall), but the engine + GUI app are gone. The orphaned `/Library/PrivilegedHelperTools/com.docker.vmnetd` process is left over from a previous Docker install.

This means **the live-bring-up steps of Phase 1 did NOT execute in this session**:

| step | status this session |
|---|---|
| `./infra/bootstrap.sh` | NOT RUN — Docker daemon unavailable |
| `pm2 start ecosystem.config.js` | NOT RUN — orchestrator would crash on DB ping without Postgres |
| `pm2 save` | NOT RUN — depends on `pm2 start` |
| `pm2 startup` | NOT RUN — depends on `pm2 save` |
| Dashboard live at :8765 | NOT REACHED |
| First commit → end-to-end loop | NOT EXERCISED LIVE |

I did NOT install Docker Desktop autonomously. Installing a major desktop application without explicit per-install authorization isn't an "execution mode" action — it's a shared-state action that affects the host machine in a way I don't want to make without Henry's go-ahead.

**Operator action required before live operations:**
```bash
brew install --cask docker
# or: download from https://www.docker.com/products/docker-desktop
open -a Docker             # accept the license prompt
docker info                # confirm daemon reachable
```

Then resume from `./infra/bootstrap.sh`.

## What I observed in this session anyway

### CIWatcher actually polls real GitHub
A smoke-run against `Wise-Est-Systems/wiseorder-protocol` via the `gh` CLI returned:
```
repo=Wise-Est-Systems/wiseorder-protocol
status=OK  note='all recent runs green'
failed_runs: 0  in_flight: 0  flaky: []
```
This is **real CI data, fetched via real subprocess, against the real org account.** Not a mocked example. The `gh` CLI's authed session is the token strategy — no separate token management in the wiseorder runtime.

### IntegrityWatcher verifies the real chain
A smoke-run of `_verify_once()` against the protocol's actual `chain/` directory:
```
status: CHAIN_VALID
count:  3
head:   5964497c48c877946e2c92d15e3116f5991c1d8a4c99dc7eadb477cec558dd81
chain:  /Users/thekingflame/Desktop/wiseorder-protocol/chain
```
Again, real bytes. Same head we've verified in every prior session.

### DailySummary 5-section format renders
Tested with both mixed-state (`4 completed, 1 failed, 2 interrupted, 1 running, 2 pending approvals, 7 ERROR lines`) and idle-state input. Both render cleanly with each section terminating in a one-line "nothing" when empty. No jargon dumping. The "Recommended next action" stays single-line and matches the highest-urgency item.

Sample output (mixed):
```
**What changed**
4 workflow(s) completed, 1 failed, 2 interrupted, 1 still running.
most common: `commit_pipeline` (5x).

**What failed**
1 workflow(s) marked failed.
dead-letter queue holds 2 job(s); see `GET /queues/failed`.
top failure shapes: `llm_failed` (2), `timeout` (1).
`logs/wiseorder.jsonl` has 7 ERROR-level line(s) in the recent tail.

**What matters most**
2 pending approval card(s) — operator action required.
2 interrupted workflow(s) — possible orchestrator restart.
2 dead-letter job(s) — investigate or discard.

**Recommended next action**
Review pending approvals first. ...

**Approval backlog**
2 pending; 3 decided (2 approved, 1 rejected).
```

### Test suite remains green
21 passed, 8 skipped (services-deferred). No regressions from the new code (DemoForgeWorker + CIWatcher + DailySummary refactor + orchestrator wiring).

## Real friction observed

These are real, not speculative.

1. **The session-time vs operational-time mismatch.** "Operate for multiple days" is the right intent for the work order. Inside a single session I can ship the *components* that would be operated; I cannot generate the *friction telemetry* that comes from days of real use. Every operational improvement that should come from this phase (noisy summaries → tighter prompts; queue drift → better tooling; approval pain points → UX changes) requires data from real runs. That data does not exist after a single session.

2. **`gh` CLI as the GitHub token strategy is a real choice with a real downside.** If `gh` is not authed, CIWatcher records an `auth_unavailable` history entry and stops being useful. In a multi-day operational scenario, an auth-token expiration would silently stop CI visibility. CIWatcher tolerates this gracefully (records ERROR, keeps polling), but the OPERATOR has to see the dashboard's `WATCHERS` card to notice. There is no proactive alert.

3. **The DemoForge bridge depends on `WISEORDER_DEMO_FORGE_DIR` resolving to a real clone of demo-forge.** If the user runs the orchestrator on a different machine where demo-forge isn't cloned, every `demo_request` job will fail at the first `make check-tools` step. Documented in the worker, but operational reality: this is a coupling between two repos that lives in an env var.

4. **PM2 startup persistence requires sudo.** `pm2 startup` prints a sudo command the operator must run once. That's documented in `ecosystem.config.js`. In a one-operator scenario it's fine; in a "give this to a teammate" scenario it's friction.

5. **`docker compose down` deletes the chromadb collection if the operator forgets the volume name.** The runtime's vector store lives in `./data/chroma/` on the host (not in a Docker volume). `docker compose down -v` only wipes the named Docker volumes; the chroma data survives. Documented in RECOVERY_MODEL. But the inverse — chroma data lives outside Docker, which means it's NOT cleaned by `docker compose down -v`. A naive operator expecting a clean reset might be surprised. Acceptable design choice; worth flagging.

## What I did NOT observe (and would need real operational time to)

- Whether the DailySummary's 09:00 UTC schedule is actually useful for an operator in non-UTC timezones. Henry is US Eastern; 09:00 UTC is 04:00 / 05:00 his time. He'd never see the summary while it's "fresh." Configurable via `WISEORDER_DAILY_SUMMARY_HOUR` but the default is wrong for his timezone.
- Whether the dashboard's 5-second polling cadence is too aggressive or too slow.
- Whether the IntegrityWatcher's 5-minute poll interval misses meaningful events (chain head moves are rare — minutes-scale polling is fine).
- Whether the CIWatcher's 10-minute interval is too noisy or too sparse.
- Whether the approval cards' Discord webhook delivery survives provider rate limits or network outages.
- Whether the `make ci` pre-flight passes under real CI load (the CI matrix passes, but `make ci` from a fresh clone hasn't been timed under realistic conditions).
- Whether `pm2 save` + `pm2 startup` actually survives a real Mac reboot.

Each is a real day-of-operation question. None can be answered in a single session.

## What changes I'd make AFTER multi-day operation

These are speculative — flagged as such — but informed by what I observed in this session:

1. **Make `WISEORDER_DAILY_SUMMARY_HOUR` default to `13` (1pm UTC = 9am ET / 6am PT).** Most operators are on US time; 09:00 UTC is 4am for the East Coast.
2. **Add a `daily_summary` button to the dashboard** that triggers `POST /summary/run-now`. Currently only via curl. Operators want a click.
3. **Surface the IntegrityWatcher's ERROR state more prominently.** Today it's a row in `/watchers`'s JSON; an operator who only checks the dashboard might miss "chain not found because protocol repo moved."
4. **Add a "pending approvals" badge in the dashboard `<title>` so it shows in the browser tab title.** Operational telemetry without checking the tab.

None of these are in scope for this session. They are real follow-ups for a future operationalization cycle.

## The operational stance, stated explicitly

**STACK_001 is a real operational stack on paper and in source code, but in this session it never ran live end-to-end.** It is one Docker-Desktop install away from doing so. The code that would run it is committed, tested, and verifiable.

If a reviewer asks "have you actually operated this?" the honest answer is: components individually, yes — CIWatcher hits real GitHub, IntegrityWatcher reads real chain bytes, DailySummary formats real (synthetic) stats, the test suite exercises the runtime end-to-end via mocks. **The full live PM2-managed deployment did not run.** That is the next operator action.
