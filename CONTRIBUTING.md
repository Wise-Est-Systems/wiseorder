# Contributing to wiseorder (runtime)

Thanks for considering a contribution. The protocol's strength is precise small contributions, not large rewrites.

## What we accept

- **Bug fixes** that include a failing test + the fix that makes it pass.
- **Conformance vectors** that exercise a specific protocol invariant. New vectors go under `vectors/v<X>/` and must include both `pass`-case and `fail`-case JSON files.
- **Verifier-track ports** in additional languages. See `docs/release/THIRD_PARTY_VERIFIER_BRIEF_v0.1.md` for the formal requirements.
- **Documentation that closes ambiguity** in existing specs. We do NOT accept doc-only PRs that add philosophy; we accept doc PRs that make a load-bearing statement testable.

## What we do not accept

- Changes to `alembic/versions/**` that are not migrations (only new migration files; never edit a committed one).
- Changes that add new dependencies without justification in `pyproject.toml` and the PR description.
- Changes that add new dependencies without a clear justification in the PR description.
- "Reformat" PRs that change >100 lines without changing behavior. We run lint; bring your changes under a lint pass.
- New "agent" abstractions, new "framework" layers, new "platform" features. The protocol is intentionally narrow.

## Minimum contributor setup

```bash
git clone https://github.com/Wise-Est-Systems/wiseorder.git
cd wiseorder-protocol
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
make bootstrap   # venv + pip install -e .[dev]
make services-up # Postgres + Redis via docker compose
make ci          # lint + test-pure (the pre-flight gate)
```

Expected: every step prints OK. If a step fails on a clean clone, that's a bug in the build — open an issue.

## Pull request checklist

Every PR must satisfy ALL of these. CI will catch most automatically; reviewers will close PRs that miss them.

- [ ] `make ci` passes locally.
- [ ] The change addresses a single concern. Bundling unrelated changes is grounds for splitting.
- [ ] If the change touches behavior: a test that fails on `main` and passes on the PR branch.
- [ ] If the change touches `intellagent_runtime/iii.py`, `chain.py`, or `memory.py`: explicit mention in the PR description. These are integrity-critical paths.
- [ ] No `TODO`, `FIXME`, or `XXX` markers in code touched by the PR. The `make no-pseudocode` check enforces this.
- [ ] Commit messages explain *why*, not just *what*. One sentence on motivation; one on mechanism.
- [ ] Branch is rebased on latest `main` (linear history; no merge commits).

## Commit message format

We do not enforce conventional-commits style strictly. We do require:

- A subject line under 70 characters.
- A blank line.
- A body that explains the motivation and any non-obvious mechanism.

Example:
```
fix(chain): handle empty audit dir without raising

verify_chain() raised IndexError when intellagent_audit/ contained no
entries. The empty case should return CHAIN_EMPTY, not crash. Added a
defensive check before the head-of-list dereference.

Test: tests/test_chain.py::test_verify_empty_dir
```

## Test discipline

- **All tests must pass on the matrix** (ubuntu/macos × py3.11/py3.12). CI gates this.
- **No `skipif` without justification.** If a test must skip on a platform, the skipif reason must explain why with one sentence.
- **No mocks of integrity primitives.** Tests of `chain.verify_chain` must use real triples and real hashes; mocking the III function is grounds for closing the PR.
- **Hardening tests live under `tests/test_hardening*.py`.** New code that touches the orchestrator's recovery paths or dedup must have a corresponding hardening test.

## Reporting bugs

Use the bug-report issue template (`.github/ISSUE_TEMPLATE/bug_report.md`). The template asks for:

1. The exact command you ran.
2. The full output (paste; don't summarize).
3. The expected output.
4. Your platform (`uname -a`) and Python version (`python3 --version`).
5. The commit hash (`git rev-parse HEAD`).

Reports that omit any of those will be closed and asked to resubmit. The constraint is not bureaucracy; it's that "couldn't reproduce" is the most common cause of orphaned issues.

## Reporting security issues

Do NOT open a public issue for a security vulnerability. See `SECURITY.md` for the private reporting path. We commit to a response within 7 days.

## Code review

- Reviews focus on: (1) correctness, (2) test coverage, (3) docs in sync with code.
- Reviews do NOT focus on: style nits beyond what ruff catches, naming preferences, architectural rewrites.
- Reviewers may ask for changes; authors can push back if the request is wrong. Disagreements get resolved in writing, not by seniority.

## License

By contributing, you agree your contribution is licensed under Apache-2.0 (the repo's license). We do not require a CLA. We expect contributors to have the legal right to license their contribution.

## What you can ignore

- "Best practices" that contradict the explicit rules above. The protocol's discipline lives in the rules above.
- The size of the existing docs. The protocol grew docs first, code second. New contributions should fit the existing code structure; new docs should match the existing format.

The shortest PR description we accept: *"Fixes issue #N. New test in tests/test_X.py demonstrates the regression. Make ci passes."*

The shortest PR description we reject: *"Improves robustness."*
