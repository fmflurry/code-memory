---
name: code-memory
description: Local-first memory layer (structural graph + semantic vectors + episodes) wired in via MCP and the OpenCode plugin. Use when you want to recall how a feature is wired, find prior work on a topic, or refresh the index after a write.
---

# code-memory

The `code-memory` plugin gives every session a small, query-relevant
**Context Pack** drawn from a local index of the codebase:

- **Code hits** — symbol-level snippets ranked by semantic similarity.
- **Prior episodes** — past task prompts and outcomes that may apply.
- **Graph neighbors** — files / symbols structurally adjacent to the hits.

The plugin tries to make memory ambient: it retrieves automatically when
the user message looks like a substantive code question, and it indexes
files automatically when they're written or edited. Manual calls are still
available through the MCP tools.

## When the plugin acts on its own

| Trigger                         | Action                                           |
| ------------------------------- | ------------------------------------------------ |
| User sends a code-shaped prompt | Pulls a Context Pack (5 min TTL) and injects it. |
| `write` / `edit` tool succeeds  | Re-indexes the affected file in the background.  |
| `session.idle` event            | Records the session as an episode (best effort). |

Trivial follow-ups ("yes", "continue", "thanks") do not trigger retrieval.

## When **you** should call the MCP tools manually

The plugin is conservative; you should still call these when appropriate:

- `codememory_retrieve(query, k?, eps?)`
  - Before deep refactors that span symbols you haven't touched.
  - When the user mentions a feature or bug "we worked on before".
  - When debugging — pull prior episodes with similar symptoms.
- `codememory_record(prompt, plan?, patch?, verdict?)`
  - At the end of a non-trivial task. Pass the patch (`git diff`) and a
    verdict (`success` / `reverted` / `partial`). Future sessions will
    surface this episode for similar prompts.
- `codememory_reingest(path)`
  - After multi-file rewrites or anything the editor hook may have missed.

## How to read a Context Pack

The pack arrives as a system-prompt block. Treat it as **orientation**, not
ground truth:

1. Skim **Code hits** for files / symbols you didn't already know about.
2. Open the highest-scoring hits and verify they're still relevant.
3. Check **Prior episodes** — if a past episode matches your task, read
   its plan / patch before reinventing it.
4. **Graph neighbors** point at callers / callees / imports of the hits.
   Useful for blast-radius questions ("who else calls this?").

If the pack is empty or low-signal, fall back to `read` / `grep` and call
`codememory_record` at the end so the next session has more to work with.

## Failure modes

- If FalkorDB, Qdrant, or Ollama is down, the CLI errors and the plugin
  silently no-ops. Manual calls return an error payload; surface it to
  the user and continue without memory.
- If the index is stale, run `code-memory ingest <repo>` in a terminal —
  the git-aware delta makes this cheap.
