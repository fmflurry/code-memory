---
name: code-memory
description: Local-first memory layer (semantic vectors + call/import graph + episodes + bi-temporal user claims) wired in via MCP. Use to orient on unfamiliar code, answer topology questions (who calls X, who imports Y), recall prior work, refresh the index after writes, AND to remember durable user assertions — preferences ("I love / prefer / want"), tech-stack decisions ("we use X"), rejections ("don't ship Y"), ownership, location facts. Load this skill whenever the user states an opinion, preference, choice, or correction worth keeping across sessions.
---

# code-memory

`code-memory` exposes a local index of the project as **four complementary
surfaces**:

1. **Orientation** — a `Context Pack` (semantic code hits + relevant past
   episodes) injected automatically when the user asks something substantive.
   Tells you *roughly where to look*.
2. **Topology** — explicit MCP tools that answer precise call-graph /
   import-graph questions. Tells you *exactly what depends on what*.
3. **.NET assembly surface** — read API members from indexed DLLs on demand
   so you can answer overload / signature questions without bulk-indexing
   millions of method symbols.
4. **Temporal** — time-travel queries against the bi-temporal code graph.
   Tells you what the code *used to look like* at a past commit, or what
   has *drifted* since the last ingest.

Use them in that order. Vectors orient; the graph answers structural
questions; assembly tools answer "what's on this .NET type"; temporal
tools answer "before / after" / "what was true at commit X".

## TL;DR for open-weight models

If the user's phrasing matches the left column, call the right tool. Do
**not** answer from grep or memory before you've tried the right tool.

| User says…                                                | First call                                                  |
| --------------------------------------------------------- | ----------------------------------------------------------- |
| "explain X" / "how does this work" / "where does it live" | `codememory_retrieve(query=user-text, project=…)`           |
| "docs inventory" / "repo documentation" / "where do docs live" | `codememory_retrieve(query=user-text, project=…)`, then `glob` / `read` to verify exhaustive list |
| "who calls X" / "what depends on X" / "impact of …"       | `codememory_callers(symbol="X", project=…)`                 |
| "what does X call" / "outgoing dependencies of X"         | `codememory_callees(symbol="X", project=…)`                 |
| "where is X defined" / "X is ambiguous"                   | `codememory_definitions(symbol="X", project=…)`             |
| "who imports M" / "who uses the package M"                | `codememory_importers(target="M", project=…)`               |
| "what does file F import"                                 | `codememory_dependencies(file="F", project=…)`              |
| "what methods does T have" (.NET) / "show overloads of T" | `codememory_assembly_members(type="T", project=…)`          |
| "what changed since" / "stale references" / "drift"       | `codememory_drift(head_sha=…, project=…)`                   |
| "what used to call X before deletion"                     | `codememory_callers_at_sha(symbol="X", sha=…, sha_ord=…)`   |
| "what existed at commit C"                                | `codememory_at_sha(sha=C, sha_ord=N, project=…)`            |
| "I just wrote / edited a file"                            | `codememory_reingest(path=…, project=…)`                    |
| "I finished a task"                                       | `codememory_record(prompt=…, patch=…, verdict=…, project=…)`|
| User asserts a durable fact / preference / decision       | `codememory_assert_claim(subject, predicate, object, project=…)` |
| "what did the user say about X" / surface user prefs      | `codememory_claims(subject="X", project=…)`                 |
| "how healthy is code-memory" / "backend status"          | `codememory_health(project=…)`                              |

If two rules match, pick the more specific one (`callers` beats `retrieve`
when the question is about a named symbol).

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

| Tool                            | Required                                       | Optional                                    |
| ------------------------------- | ---------------------------------------------- | ------------------------------------------- |
| `codememory_retrieve`           | `query`, `project`                             | `k` (default 8), `eps` (5), `include_idle_episodes` (false) |
| `codememory_record`             | `prompt`, `project`                            | `plan`, `patch`, `verdict`                  |
| `codememory_reingest`           | `path`, `project`                              | —                                           |
| `codememory_ingest`             | `root`, `project`                              | `full`, `since`, `dry_run`, `confirmed` — **never call without explicit user authorisation** |
| `codememory_callers`            | `symbol`, `project`                            | `depth` (1–3, default 1)                    |
| `codememory_callees`            | `symbol`, `project`                            | `depth` (1–3, default 1)                    |
| `codememory_importers`          | `target`, `project`                            | —                                           |
| `codememory_dependencies`       | `file`, `project`                              | `depth` (1–3, default 1)                    |
| `codememory_definitions`        | `symbol`, `project`                            | —                                           |
| `codememory_assembly_members`   | `type`, `project`                              | `assembly` (`'Name, Version=…'`)            |
| `codememory_drift`              | `head_sha`, `project`                          | —                                           |
| `codememory_at_sha`             | `sha`, `project`                                | `sha_ord` (auto-computed if omitted), `label` (`Symbol`/`File`, default Symbol), `limit` (200) |
| `codememory_callers_at_sha`     | `symbol`, `sha`, `project`                      | `sha_ord` (auto-computed if omitted)         |
| `codememory_assert_claim`       | `subject`, `predicate`, `object`, `project`    | `polarity` (true), `confidence` (0.95), `evidence_span`, `valid_at`, `session_id`, `source_prompt_id` |
| `codememory_claims`             | `project`                                      | `subject`, `as_of`, `limit` (50)            |
| `codememory_health`              | `project`                                      | —                                           |

`symbol` is a bare identifier (`getBearerToken`), not a dotted expression.
`target` for `importers` is the literal module key (`@scope/pkg`, `rxjs`,
or `./relative-path`). `path` / `file` are absolute filesystem paths.
`type` for `assembly_members` is the fully qualified .NET type name
(`Namespace.TypeName`). `sha_ord` is the topological ordinal — the server
auto-computes it from the SHA if omitted; you can still pass it explicitly
to save a shell round-trip.

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

**Example:** `codememory_callers(symbol="getBearerToken", project="sample-webapp")`
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

**Example:** `codememory_importers(target="@acme-ng/security", project="sample-webapp")`.

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

## .NET assembly surface

### `codememory_assembly_members(type, project, assembly?)`

Returns the public methods declared on a Type from an indexed .NET
Assembly. Members are **not** bulk-indexed — that would multiply the graph
by 50–100× for a typical solution — so the tool reads them on demand from
the referenced DLL (~tens of ms per call).

**Call when:**
- The user is working in C# / VB / F# and asks about a method signature,
  overload, or available API surface of a referenced type.
- You see a `name::SomeMethod` placeholder in topology output and want to
  resolve which assembly exposes it.
- You need to disambiguate "which overload was called" before reading
  source.

**Don't call when:**
- The type is defined in the user's own source code — `codememory_definitions`
  is faster and gives line ranges.

**Example:** `codememory_assembly_members(type="System.Linq.Enumerable", project="my-dotnet-app")`

If `assembly` is omitted, the first matching assembly wins. Pass the full
identity (`'Name, Version=X.Y.Z.W, Culture=…, PublicKeyToken=…'`) when
multiple versions of the same DLL are referenced.

## Temporal — time-travel queries

The graph stamps every File / Symbol / edge with `first_seen_sha` /
`last_seen_sha` / `invalid_sha` (and matching topological ordinals).
Deletes don't erase data; they tombstone it. These three tools query the
history that builds up.

### `codememory_drift(head_sha, project)`

Symbols the most recent ingest didn't confirm at `head_sha`. Each row is
either **`tombstoned`** (explicitly removed) or **`drifted`** (an
incremental ingest missed it).

**Call when:**
- The user asks "what's stale" / "what changed" / "is the index up to
  date".
- A comment or doc references a symbol you suspect no longer exists.
- After a long-running watcher session, to sanity-check coverage.

### `codememory_at_sha(sha, sha_ord, project, label?, limit?)`

Lists nodes alive at the supplied commit. Pass `sha_ord` (precomputed
once with `git rev-list --count --first-parent <sha>`).

**Call when:**
- The user asks "what existed in release/26.18" or any "at commit X".
- You want to reconstruct the symbol surface of a historic version
  without checking out the worktree.

**Caveat:** only nodes that carry topological ordinals are visible — anything ingested before the temporal upgrade is filtered out.

### `codememory_callers_at_sha(symbol, sha, sha_ord, project)`

Callers of a symbol **as the graph looked at that commit** — including
tombstoned edges that were alive then.

**Call when:**
- The user asks "what used to call X before commit Y deleted it" / "who
  was using X in release/26.18".
- You need pre-deletion impact context without re-ingesting an old SHA.

## Claims — durable user assertions (you triage)

Claims are bi-temporal `(subject, predicate, object)` facts about the
**user, project, or preferences** — not about the code itself. They
persist across sessions so a future session can answer "what did the
user say about X?" without re-reading every prompt.

**You are the triage layer.** Auto-extraction is intentionally OFF —
running a local LLM on every prompt produced noisy / empty triples
and missed half the durable signal because the LLM lacked task
context. You see the full conversation, so you decide.

### When to call `codememory_assert_claim`

Call it as soon as the user states something **durable** — i.e. the
assertion is likely still true in the next session, not transient task
state. Do this *inline in the same turn* as the user message; don't
batch to end-of-session.

**Worth asserting (call the tool):**
- Tech-stack decisions: "we use Postgres / FalkorDB / Tailwind".
- Stable preferences: "I prefer terse output", "always use TDD".
- Ownership / location: "the auth service is in apps/api/auth",
  "Alice owns the billing module".
- Rejections: "don't ship dark mode", "we're not using Redis here".
- Explicit corrections of your behavior that should persist:
  "stop summarizing at end of every turn" → `(user, prefers,
  "no end-of-turn summaries")`.
- Deployment / environment facts: "we deploy to Fly.io".

**Not worth asserting (skip the tool):**
- Questions ("should we use X?") — no assertion.
- Hypotheticals ("if we used X…") — no assertion.
- Transient task state ("fix this bug now") — covered by
  `codememory_record`, not claims.
- Info already derivable from code (package.json, imports) — read the
  file instead.
- Opinions about third parties ("React is overrated") — too noisy.
- Small talk / acknowledgments.

### Predicate vocabulary (kebab-case)

Use these canonical predicates so single-valued contradiction
handling works:

| Predicate         | Sense                                  | Single-valued? |
| ----------------- | -------------------------------------- | -------------- |
| `uses`            | primary tool ("we use Postgres")       | yes            |
| `prefers`         | user preference                        | yes            |
| `rejected`        | explicit no                            | no             |
| `wants-to`        | stated intent                          | no             |
| `is-located-at`   | path / URL                             | yes            |
| `depends-on`      | hard dependency                        | yes            |
| `deployed-to`     | deployment target                      | yes            |
| `owns`            | ownership / responsibility             | yes            |
| `is-a`            | type / category                        | yes            |
| `mentioned`       | weak co-occurrence (use sparingly)     | no             |
| `worked-on`       | history of work                        | no             |

Single-valued predicates auto-close prior conflicting assertions: a
later `(project, uses, "FalkorDB")` supersedes an earlier
`(project, uses, "Neo4j")` without manual cleanup.

### Subject conventions

- `user` — the human at the keyboard.
- `project` — the current repo.
- Bare identifiers for files, modules, services
  (`apps/api/auth`, `BillingService`).
- Person names for ownership claims (`Alice`).

### `evidence_span`

Pass the verbatim user quote that justifies the claim when
practical. It's not enforced but it makes future audit trivial.

### `codememory_claims` — read user preferences

Call before making a recommendation that touches a user preference
area (deployment, tooling choice, output style). Example:
`codememory_claims(subject="user", project=…)` returns the agent's
running profile of the user's stated preferences.

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
   ├─ Question mentions a past commit / "before" / "used to" / "drift" /
   │   "release/X"?
   │   → codememory_drift  (current vs last ingest)
   │   → codememory_at_sha / codememory_callers_at_sha  (point-in-time)
   │
   ├─ Question about a .NET type's method surface (overloads, signatures)?
   │   → codememory_assembly_members(type=Namespace.Name)
   │
   ├─ Topology question ("who calls", "what imports", "impact",
   │   "who defines")?
   │   → codememory_callers / callees / importers / dependencies / definitions
   │   → ONLY THEN open files at the lines the graph returned
   │
   ├─ Need conceptual orientation in unfamiliar area?
   │   → codememory_retrieve(query)
   │
   ├─ User asserted a durable preference / decision / ownership /
   │   rejection?
   │   → codememory_assert_claim(subject, predicate, object, project)
   │     inline in the same turn (don't batch)
   │
   ├─ About to make a recommendation that touches a known preference
   │   area (deploy target, output style, tooling)?
   │   → codememory_claims(subject="user", project=…) first
   │
   └─ After completing the task → codememory_record(prompt, patch, verdict)
```

**Cost ordering (cheapest first):** drift / at_sha → topology graph hops
→ assembly_members (reads a DLL) → retrieve (vector + rerank) → reading
source files. Prefer the cheaper tool when both would answer.

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
- If `codememory_at_sha` returns `[]` for a SHA you know is real, the
  graph rows from that era predate the temporal upgrade — the lifecycle
  fields are NULL so the query filters them out. Either re-ingest at
  that SHA or fall back to `git show <sha>:<file>` for source-level
  answers.
- If `codememory_assembly_members` returns an error like "no parsable
  DLL", the assembly was referenced but not indexed (no NuGet restore
  ran, or the project was excluded). Don't retry; surface the gap to
  the user.

## Never call without authorisation

- **`codememory_ingest`** triggers a full / incremental repository
  ingest. On large repos this takes minutes to hours and blocks the MCP
  transport. **Always confirm with the user first**; only set
  `confirmed=true` after they explicitly authorise the run in chat. The
  server returns a dry advisory payload when `confirmed` is omitted.
