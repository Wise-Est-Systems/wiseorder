# BRANCH_PROTECTION

Operational policy for `main` on `wiseorder` (operational runtime). Less
strict than the protocol's policy because nothing here is cryptographically
load-bearing — but CI must still gate merges.

## Required

| rule | rationale |
|---|---|
| Required status checks: `tests / pytest ubuntu-latest py3.12`, `migration-check / alembic upgrade head from empty schema` | Tests prove the code runs; migration-check proves the schema evolves cleanly. |
| Block force pushes to `main` | Force-push to `main` rewrites operator runbooks (release tags, deployment markers). |
| Require linear history | Easier rollback; clearer git log. |

## Recommended

| rule | rationale |
|---|---|
| Require at least 1 review on PRs touching `alembic/versions/**`, `core/memory/models.py`, `docker-compose.yml`, `pyproject.toml` | Schema and dependency changes deserve a second pair of eyes. |
| Use squash-merge by default | Keeps history readable. |
| Use signed tags for releases (`v*`) | Provenance for any external operator who pulls the repo. |

## Forbidden

| rule | rationale |
|---|---|
| **NEVER** commit `.env` files | Real secrets in git history are unrecoverable; rotate every key if it ever happens. |
| **NEVER** commit `data/chroma/` contents | Embedded vector store; bloats the repo and is regenerable. |
| **NEVER** disable a CI gate to ship | The CI is the gate. If it's broken, the deploy is broken. |

## How to enable on GitHub

```
Settings → Branches → Branch protection rules → Add rule
  Branch name pattern: main
  ☑ Require a pull request before merging
    ☑ Require approvals: 1
  ☑ Require status checks to pass before merging
    Add: tests / pytest ubuntu-latest py3.12
    Add: migration-check / alembic upgrade head from empty schema
  ☑ Require linear history
  ☐ Allow force pushes  (must remain unchecked)
  ☐ Allow deletions     (must remain unchecked)
```
