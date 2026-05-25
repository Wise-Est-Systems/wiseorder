# infra/

Foundation services + bootstrap scripts for STACK_001.

This directory is the **deployment layer** for the WiseOrder operational
stack. It's deliberately separate from application code (`core/`, `agents/`,
`workflows/`) so a sysadmin can read it in one sitting without touching
the Python codebase.

## Layout

```
infra/
├── README.md             this file
├── docker-compose.yml    Postgres + Redis (canonical)
├── bootstrap.sh          one-command stack bring-up
├── healthcheck.sh        verify services + orchestrator state
├── env/                  per-environment override files (.env.production, etc.)
└── persistence/          on-disk volume mount points (created at runtime)
```

The `docker-compose.yml` here is the canonical compose. The repo root
also has a `docker-compose.yml` (preserved for the existing Makefile's
`make services-up`); they point to the same services on the same ports.
Edit this one if you need to change the deployment shape.

## Quick start

```bash
cd infra
./bootstrap.sh
```

The bootstrap script:
1. Verifies Docker daemon is running.
2. Brings up Postgres + Redis (`docker compose up -d`).
3. Waits for both health checks to pass.
4. Reports the URLs.

Run `./healthcheck.sh` at any time to verify the stack is healthy without
disrupting it.

## What lives here vs in the app

| in `infra/` | NOT in `infra/` |
|---|---|
| `docker-compose.yml` | Python source code |
| `bootstrap.sh` | Alembic migrations (live in `alembic/`) |
| `healthcheck.sh` | LLM prompts |
| Future: `pm2-ecosystem.config.js` | FastAPI routes |
| Future: per-env `.env` files | tests |

The boundary is: anything a sysadmin would need to bring the stack up
on a new machine lives here. Anything a developer would need to change
the system's behavior lives in the application directories.
