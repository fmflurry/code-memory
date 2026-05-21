---
description: Manually drive the code-memory backend (retrieve / record / reingest / resolve / ingest).
argument-hint: <retrieve|record|reingest|resolve|ingest> [args...]
allowed-tools: Bash(code-memory:*)
---

Forward $ARGUMENTS to the local `code-memory` CLI.

Use this when:

- You want to force a Context Pack retrieval for an explicit query
  (`/code-memory retrieve "rotate refresh tokens" --json`).
- You finished a non-trivial task and want to record an episode with a
  custom verdict / plan / patch (`/code-memory record --prompt "..." --verdict success`).
- You suspect the index is stale after an out-of-band edit
  (`/code-memory ingest .`) or want to force a full re-resolve
  (`/code-memory resolve`).

The plugin's hooks already cover the automatic paths (per-prompt
retrieval, post-write reingest, debounced resolver, idle record). This
command exists for the few cases the agent needs to drive the backend
manually.

!`code-memory $ARGUMENTS`
