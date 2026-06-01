---
canonical-name: wiseorder
layer: runtime
parent: Wise-Est-Systems
license: Apache-2.0
canon: https://github.com/Wise-Est-Systems/wiseorder-protocol/blob/main/STRUCTURE.md
---

# Role: Operational Runtime

`wiseorder` is the **operational runtime** of the Wise.Est Systems stack.

## What this repo IS

- A single Python process implementing one event-driven workflow: `commit_pipeline` (commit → engineering summary → social draft → human approval card).
- A FastAPI server bound to `127.0.0.1:8765` by default. Remote bind requires explicit opt-in and a token.
- Storage in Postgres + Redis + embedded ChromaDB. No microservices. No queue broker beyond Redis lists.
- An EventWatcher for filesystem events, async workers for pipeline execution, and a dashboard for human approval gates.

## What this repo IS NOT

- A governance kernel. That is `wiseorder-protocol`.
- A multi-agent framework. There are no agent classes, no agent-to-agent messaging, no agent supervision tree.
- A general workflow engine. Exactly one pipeline (`commit_pipeline`) is registered; adding more is a small change but not the point.
- A production-grade auth surface.

## Drift policy

Any change to this file MUST be accompanied by an update to the `wiseorder` row in [`wiseorder-protocol/STRUCTURE.md`](https://github.com/Wise-Est-Systems/wiseorder-protocol/blob/main/STRUCTURE.md). CI verifies the fingerprint on every push.
