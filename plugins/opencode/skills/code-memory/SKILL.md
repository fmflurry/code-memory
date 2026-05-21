---
name: code-memory
description: Local-first memory layer (semantic vectors + call/import graph + episodes) wired in via MCP and the OpenCode plugin. Use to orient on unfamiliar code, answer topology questions (who calls X, who imports Y), recall prior work, and refresh the index after writes.
---

# code-memory

`code-memory` exposes a local index of the project as **two complementary
surfaces**:

1. **Orientation** — a `Context Pack` (semantic code hits + relevant past
   episodes) injected automatically into the system prompt when the user
   asks something substantive. Tells you *roughly where to look*.
2. **Topology** — five explicit MCP tools that answer precise
   call-graph / import-graph questions. Tells you *exactly what depends on
   what*.

Use both. Vectors orient; the graph answers structural questions.

## Parameter contract — read this first

**Every `codememory_*` MCP tool requires a `project` parameter.** This is
non-negotiable; the server rejects calls that omit it. The error message
returned in that case tells you which slug to pass.

### How to find the right slug

1. Look at the **previous tool response** — every successful call echoes
   `"project": "<slug>"`. Reuse that exact value.
2. If you haven't called any tool yet, look at the `project` field
   description in any tool's inputSchema — the server embeds the current
   project's slug there as `currently: \`<slug>\``.
3. As a last resort, list known slugs from the shell: `code-memory projects`.

### Forbidden values

These are sentinel strings the server explicitly rejects:

- `"auto"` — not a slug. Falls back to nothing.
- `"default"` — same.
- `""` / `null` / whitespace-only — same.

If you don't know the slug, **don't invent one**. Re-read this skill or
inspect a prior tool response.

### Full parameter reference

| Tool                       | Required                                       | Optional                                    |
| -------------------------- | ---------------------------------------------- | ------------------------------------------- |
| `codememory_retrieve`      | `query`, `project`                             | `k` (default 8), `eps` (5), `include_idle_episodes` (false) |
| `codememory_record`        | `prompt`, `project`                            | `plan`, `patch`, `verdict`                  |
| `codememory_reingest`      | `path`, `project`                              | —                                           |
| `codememory_callers`       | `symbol`, `project`                            | `depth` (1–3, default 1)                    |
| `codememory_callees`       | `symbol`, `project`                            | `depth` (1–3, default 1)                    |
| `codememory_importers`     | `target`, `project`                            | —                                           |
| `codememory_dependencies`  | `file`, `project`                              | `depth` (1–3, default 1)                    |
| `codememory_definitions`   | `symbol`, `project`                            | —                                           |

`symbol` is a bare identifier (`getBearerToken`), not a dotted expression.
`target` for `importers` is the literal module key (`@scope/pkg`, `rxjs`,
or `./relative-path`). `path` / `file` are absolute filesystem paths.

## Auto-injected Context Pack

| Trigger                         | Action                                           |
| ------------------------------- | ------------------------------------------------ |
| User sends a code-shaped prompt | Pulls a Context Pack (5 min TTL) and injects it. |
| `write` / `edit` tool succeeds  | Re-indexes the affected file in the background.  |
| `session.idle` event            | Records the session as an episode (best effort). |

Trivial follow-ups ("yes", "continue", "thanks") do not trigger retrieval.

The pack contains:
- **Code hits** — symbol-level snippets ranked by semantic similarity.
  Treat as candidates, not answers.
- **Prior episodes** — past task prompts + verdicts that may apply.

If the pack is empty or low-signal, fall back to graph tools first (cheap,
exact) and then `read` / `grep` only as a last resort.

## Topology tools — call these autonomously

Before reading multiple files, ask whether a single graph query would
answer the question precisely.

### `codememory_callers(symbol, project, depth?=1)`

Who calls this symbol? Reverse `CALLS` traversal.

**Call when:**
- Asked "what depends on X" / "what uses X" / "impact of renaming X".
- About to refactor or rename a function/method/class.
- Need to estimate blast radius before a change.

**Example:** `codememory_callers(symbol="getBearerToken", project="gc-webapp")`
→ list of files + the definition's location.

### `codememory_callees(symbol, project, depth?=1)`

What does the file defining this symbol call? Forward `CALLS` traversal.

**Call when:**
- Mapping the outgoing dependencies of a service or class.
- Want to know which collaborators a unit reaches.

### `codememory_importers(target, project)`

Which files import this module or relative path? Reverse `IMPORTS`.

**Call when:**
- Asked "who uses `@scope/lib`" or any package.
- Auditing impact of removing or replacing a barrel/module.
- Checking which files depend on a shared utility.

**Example:** `codememory_importers(target="@internal-ng/security", project="gc-webapp")`.

### `codememory_dependencies(file, project, depth?=1)`

What modules does this file import? Forward `IMPORTS`.

**Call when:**
- Triaging an unfamiliar file — start with its external surface.
- Looking for hidden coupling before changing a file.

### `codememory_definitions(symbol, project)`

Every file+line that defines a symbol with this name.

**Call first** when a name is ambiguous, before passing it to
`callers` / `callees`. Tells you whether the symbol is unique or
duplicated across modules.

## Manual orientation + write tools

- `codememory_retrieve(query, project, k?, eps?, include_idle_episodes?)`
  - Force orientation for a tricky query (e.g. conceptual question with
    no obvious keyword).
- `codememory_record(prompt, project, plan?, patch?, verdict?)`
  - **Call at the end of any non-trivial task.** Pass the patch (`git
    diff`) and a verdict (`success` / `reverted` / `partial`). Future
    sessions will surface this episode for similar prompts.
- `codememory_reingest(path, project)`
  - After multi-file rewrites or anything the editor hook may have missed.

## Decision flow

```
1. User asks question
   │
   ├─ Context Pack already injected? → skim Code hits + Episodes
   │
   ├─ Topology question detected ("who calls", "what imports", "impact")?
   │   → call codememory_callers / importers / definitions FIRST
   │   → only then open files at the lines the graph returned
   │
   ├─ Need conceptual orientation in unfamiliar area?
   │   → codememory_retrieve(query)
   │
   └─ After completing the task → codememory_record(...)
```

## How to read a Context Pack

Treat it as **orientation**, not ground truth:

1. Skim **Code hits** for files / symbols you didn't already know about.
2. Open the highest-scoring hits and verify they're still relevant.
3. Check **Prior episodes** — if a past `verdict=success` episode matches
   your task, read its plan / patch before reinventing it.
4. For "who calls / imports / defines" follow-ups, **use the topology
   tools** — never grep when one Cypher hop suffices.

## Failure modes

- **`project` missing or invalid** → server raises `MissingProjectError`
  with the cwd-detected slug embedded. Read the error, re-issue the call
  with that exact `project` value. Do not invent a slug or pass `"auto"`.
- If FalkorDB, Qdrant, or Ollama is down, the CLI errors and the plugin
  silently no-ops. Manual tool calls return an error payload; surface it
  to the user and continue without memory.
- If the index is stale, run `code-memory ingest <repo>` — the git-aware
  delta makes this cheap.
- If a topology tool returns `[]`, the symbol may be ambiguous,
  external, or simply not yet resolved. Try `codememory_definitions`
  first to disambiguate before falling back to grep.
