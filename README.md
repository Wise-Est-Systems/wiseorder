# WiseOrder Runtime v0.1

Minimal AI-native operational infrastructure. One workflow:

```
git commit  →  event detected  →  task queued  →
engineering summary  →  social post draft  →  approval request
```

That is the entire v0.1 target. Nothing more.

---

## What's inside

| Layer | What it is in plain English | Where |
|---|---|---|
| Orchestrator | The brain. Runs workers, watches for events, serves the dashboard. | `core/orchestrator/main.py` |
| Event watcher | Notices when a git commit happens in repos you tell it about. | `core/events/watcher.py` |
| Task queue | A list of jobs to do, kept in Redis. | `core/queues/redis_queue.py` |
| Memory | Postgres for structured data; ChromaDB for "search by meaning". | `core/memory/` |
| Approval gateway | Sends each result to Discord/Telegram (or just logs it). | `core/approvals/gateway.py` |
| Engineering summarizer | Reads a git diff → emits a short technical summary + risk level. | `agents/engineering/summarizer.py` |
| Social drafter | Turns that summary into one short post (≤280 chars). | `agents/social/post_generator.py` |
| Commit pipeline | The actual workflow that chains the above. | `workflows/commit_pipeline.py` |
| Dashboard | A tiny local web page at `http://127.0.0.1:8765` showing everything. | `api/server.py` |

---

## Quickstart

### 1. Bring up the services (Postgres + Redis)

```bash
cd ~/Desktop/wiseorder
docker compose up -d
```

Postgres lives on port **5433** (so it doesn't fight any Postgres you already have).
Redis lives on port **6380**.

Plain English: this starts two tiny database programs in the background.

### 2. Install Python deps

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp .env.example .env
```

Then edit `.env`:
- Set `ANTHROPIC_API_KEY=...` (or `OPENAI_API_KEY=...` and change `WISEORDER_LLM_MODEL`)
- Set `WISEORDER_WATCH_PATHS=/path/to/repo1,/path/to/repo2`
- Optional: set `WISEORDER_DISCORD_WEBHOOK_URL` to get phone-pushable approval cards

### 4. Run

```bash
python -m core.orchestrator.main
```

You should see structured JSON logs. Open `http://127.0.0.1:8765` for the dashboard.

### 5. Trigger it

Make a commit in any watched repo. Within ~1 second:
- a `commit_pipeline` job appears in the queue
- a workflow row appears in Postgres
- the engineering summary + social draft are generated
- an approval card hits the dashboard (and Discord/Telegram if configured)

---

## First-success checklist (v0.1 done condition)

- [ ] Commit in a watched repo is detected automatically
- [ ] A queue task is created
- [ ] An engineering summary is generated
- [ ] A social post draft is generated
- [ ] An approval notification appears (dashboard + Discord/Telegram if set)

---

## Tests

```bash
pytest -v
```

`test_smoke.py` runs with no services. `test_workflow.py` will auto-skip if Postgres/Redis aren't up; bring them up with `docker compose up -d` to exercise the full pipeline.

---

## Tearing it down

```bash
docker compose down            # stop services, keep data
docker compose down -v         # stop services, wipe data volumes
```

---

## Files NOT auto-created

- `.env` — copy from `.env.example` and fill in
- `data/chroma/` — gets created on first run
- `logs/*.jsonl` — gets created on first run
