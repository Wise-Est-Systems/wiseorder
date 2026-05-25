---
name: Bug report
about: Report a defect with a clear reproduction
title: "[BUG] <one-line summary>"
labels: bug
assignees: ''
---

## What I ran

```
<exact command line, including any environment variables>
```

## What I observed (full output, not summarized)

```
<paste full stdout + stderr here>
```

## What I expected

A one-line statement of what should have happened.

## Environment

- OS: `<output of uname -a>`
- Python: `<output of python3 --version>`
- Commit: `<output of git rev-parse HEAD>`
- Installed via: `[ ] pip install -r requirements.txt` / `[ ] pip install -e .` / `[ ] other (describe)`

## Reproduction steps

A minimal sequence of commands that reproduces the bug from a clean clone:

```bash
git clone https://github.com/Wise-Est-Systems/wiseorder-protocol.git /tmp/check
cd /tmp/check
# ...
```

## Anything else

If you've already attempted a fix, link the branch. If you've already opened a PR, this issue can be closed in favor of the PR.
