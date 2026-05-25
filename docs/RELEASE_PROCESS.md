# RELEASE_PROCESS

A WiseOrder Runtime release is a tag on `main`. Unlike the protocol, the
runtime has no chain artifacts — its releases are pure software releases.

## Versioning

`vMAJOR.MINOR.PATCH` standard semver:

- **MAJOR** bumps for schema-breaking changes (a migration that requires
  a manual data migration step), CLI removal, or the operator behavior
  contract changing (e.g., default bind moving off `127.0.0.1`).
- **MINOR** for new env vars, new endpoints, new optional integrations.
- **PATCH** for bugfixes.

## Pre-release checklist

1. **Clean working tree**
   ```
   git status                    # must be clean
   ```
2. **CI green on main** — every workflow in `.github/workflows/` shows
   green on the commit you intend to tag.
3. **Local CI pre-flight**
   ```
   make ci                       # lint + test-pure
   ```
4. **Service-dependent tests pass** (run locally with services up):
   ```
   make services-up
   make migrate                  # bring schema to head
   make test                     # full suite including integration tests
   ```
5. **Migration round-trip**
   ```
   alembic downgrade base && alembic upgrade head
   ```
   Both directions must complete cleanly.
6. **Probe services from the orchestrator's view**
   ```
   make probe-services
   ```
   Both DB and Redis must report OK.

## Tagging

```
git tag -s v0.1.1 -m "release v0.1.1: <summary>"
git push --tags
```

Signed tags strongly preferred. Do NOT move a tag once pushed.

## Post-release verification

1. Clone the tag in a fresh directory:
   ```
   git clone --branch v0.1.1 git@github.com:Wise-Est-Systems/wiseorder.git /tmp/check
   cd /tmp/check && make bootstrap && make services-up && make ci
   ```
2. Start the orchestrator against a fresh `.env`, hit `/healthz` and
   `/ready`, and confirm both return 200.
3. Make a synthetic commit in a watched repo and confirm a workflow
   appears on the dashboard.

If anything fails, do not move the tag — ship `v0.1.2` with the fix.

## Release notes template

```
# v0.1.1 — <one-line summary>

## Behavior changes
- <bullet list; mark BREAKING explicitly>

## New env vars
- <list>

## Migrations
- alembic revision: <id>
- requires manual step: yes / no

## CI
- all workflows green on <commit sha>
```

## Rollback strategy

Schema rollback path: `alembic downgrade <revision>` for the migration
introduced in the broken release. If data has been written under the new
schema, this may be lossy — the operator must decide whether to drop the
new rows or migrate them forward in a hotfix release.

Code rollback: `git revert <merge commit>` is preferred over force-push.
Tag the revert as the next patch version (`v0.1.2 = revert v0.1.1`).
