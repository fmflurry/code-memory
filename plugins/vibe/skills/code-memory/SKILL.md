---
name: code-memory
description: Orient on the codebase via the code-memory MCP index before grep/read/shell, and drive the backend manually (retrieve / record / reingest / ingest).
user-invocable: true
---

# code-memory: retrieve before grep

For any question that maps to "where is X / how does Y work / who calls Z /
where do docs live / what depends on this", call the `code-memory` MCP tools
**before** grep, glob, read, or shell. The index is faster, ranked, and aware
of past episodes + durable user claims.

Mistral Vibe has no lifecycle hooks, so the per-turn automation that the Claude
Code / Cursor plugins get for free does not fire here. This skill is the
standing instruction that replaces it: you drive the index explicitly.

## Pick the right tool

- `codememory_retrieve` — semantic + episodic recall. Default first call for
  any natural-language code question.
- `codememory_definitions(symbol)` — exact file + line for a name.
- `codememory_callers(symbol)` / `codememory_callees(symbol)` — call graph
  (rename impact, dependency mapping).
- `codememory_importers(target)` / `codememory_dependencies(file)` — module
  import graph.
- `codememory_at_sha`, `codememory_callers_at_sha`, `codememory_drift` —
  temporal queries (what existed at a past commit, what's stale).
- `codememory_assert_claim` — record durable user assertions (preferences,
  decisions, rejections, ownership, location).

Every tool takes a required `project` argument: the repo slug, which is the
basename of the repo root directory (e.g. `code-memory` for
`/Users/you/Workspace/code-memory`). Do not pass `auto` or `default` — they are
rejected.

## Workflow

1. Read the question. If it's about repo / code / docs structure, call the
   matching code-memory tool first.
2. Use filesystem tools (read / grep / shell) only to verify or read specific
   files the index pointed to.
3. After completing a non-trivial task, call `codememory_record(prompt, plan,
   patch, verdict)` so the next session can recall what worked.
4. When the user states a preference, decision, or rejection, call
   `codememory_assert_claim` to make it durable.

Default to one targeted MCP call over a wide grep. Read source files only after
the graph tells you exactly which lines to open.

## Driving the backend manually

The CLI covers the paths Vibe cannot hook. Run these via shell when needed:

- Force a retrieval: `code-memory retrieve "rotate refresh tokens" --json`
- Record an episode: `code-memory record --prompt "..." --verdict success`
- Refresh a stale index after an out-of-band edit: `code-memory ingest .`
- Re-point cross-file CALLS edges: `code-memory resolve`

If the OS watcher is installed (`code-memory autostart status`), file edits are
re-ingested automatically in the background and you rarely need step 3's manual
`ingest`. If it is not running, re-ingest after a batch of writes.
