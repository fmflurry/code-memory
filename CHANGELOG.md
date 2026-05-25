# Changelog

High-level notes on what changed and **why**. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with an extra
"Reason" line per entry because the *why* matters more than the *what*
when the repo grows.

This file complements `git log`: commits explain mechanics, this file
explains intent.

## Unreleased

### Added — Topological SHA ordinals + time-travel queries

**What:** every temporal stamp (`first_seen_sha`, `last_seen_sha`,
`invalid_sha`) now has a matching integer companion
(`first_seen_ord`, `last_seen_ord`, `invalid_ord`) sourced from
`git rev-list --count --first-parent <sha>`. Tombstone writes also
record an `invalid_at` wall-clock timestamp.

Two new `FalkorStore` methods read this:

- `at_sha(sha, sha_ord, label="Symbol")` — returns nodes that were
  alive at a given commit. "Alive" = `first_seen_ord <= sha_ord` AND
  (`invalid_ord IS NULL` OR `invalid_ord > sha_ord`).
- `callers_at_sha(symbol, sha, sha_ord)` — callers of a symbol as the
  graph looked at that commit. Tombstoned callers whose `invalid_ord >
  sha_ord` count as alive.

**Reason:** plain SHA strings can't be range-queried — `'abc123' <=
'def456'` is meaningless graph topology. The first-parent ordinal gives
a monotonic integer along the main branch (parent < child, always)
which makes "graph as of last week" answerable in one Cypher query
without resolving topology on the fly.

**Reason it's first-parent only:** merge commits otherwise inflate the
ordinal in ways that depend on which side of the merge a fact came
from. First-parent count tracks the *trunk* timeline, which is what
"before / after release N" usually means.

**Compatibility:** rows ingested before this slice carry no ordinal.
`at_sha` filters them out (`first_seen_ord IS NOT NULL` guard) — we can
prove a row is alive at SHA X only if we have an ordinal to compare
against. After one ingest at the post-upgrade HEAD, every touched row
backfills its ordinals via the COALESCE rules in `upsert_nodes` /
`upsert_edges`.

### Added — `code-memory vacuum` and graph tombstone GC

**What:** new CLI command and `FalkorStore.vacuum()` method that drop
tombstoned graph elements under three mutually exclusive policies:

```bash
code-memory vacuum --before release/26.18   # drop tombstones whose
                                            # invalid_ord <= ord(release/26.18)
code-memory vacuum --older-than 30d         # drop tombstones older than 30 days
code-memory vacuum --all                    # drop every tombstone
code-memory vacuum --before main --dry-run  # report counts without writing
```

Returns `{"files": N, "symbols": N, "edges": N}` of items affected.

**Reason:** the temporal model is monotonic-add-only by design (delete
is replaced by tombstone). Without vacuum, the graph grows forever.
Three policies because three workflows want different things:

- `--before` for "this release is shipped, I don't care about its
  pre-release history" — exact, reproducible, scriptable.
- `--older-than` for "I just want to bound storage, don't care which
  commit" — works in repos with no clean release pattern.
- `--all` for "nuke history, start fresh on tombstones" — useful after
  large refactors or schema migrations.

**Reason it's a separate command, not auto-run:** vacuuming is
destructive. Once a tombstone is gone, the time-travel and pre-deletion
queries that motivated the temporal model in the first place stop
working for that range. Making it explicit forces a deliberate decision.

### Added — MCP tools `codememory_drift`, `codememory_at_sha`, `codememory_callers_at_sha`

**What:** three new MCP tool surfaces wrap the temporal queries so a
coding agent can ask them directly:

- `codememory_drift(head_sha, project)` — symbols not last-seen at
  `head_sha`. Returns `tombstoned` vs `drifted` classification.
- `codememory_at_sha(sha, sha_ord, label?, limit?, project)` —
  generic "alive at this commit" query. `label` switches between
  Symbol (default) and File. Callers compute `sha_ord` via git on
  their side (we don't want the MCP server shelling out per call).
- `codememory_callers_at_sha(symbol, sha, sha_ord, project)` —
  callers as of a specific commit.

**Reason:** these queries are useless if only humans on the CLI can
reach them. Coding agents — Claude Code, OpenCode, Cursor — talk to
code-memory through MCP. Without these tools, the agent's path to
"what called X before commit Y removed it" is "shell out, checkout,
re-ingest, query, restore" — which it can't safely do. Native MCP
makes the time-travel queries first-class.

**Reason the caller passes `sha_ord` instead of having us resolve it:**
the MCP server may not have git available (sandboxed containers, MCP
running on a different host than the repo). The agent already has the
repo open and can call `git rev-list --count --first-parent <sha>`
cheaply; making it part of the input keeps the server pure.

### Added — Temporal model for the code graph

**What:** every File / Symbol / edge in the FalkorDB graph now carries
three SHA-keyed lifecycle properties:

| property | meaning |
|---|---|
| `first_seen_sha` | git HEAD at the first ingest that saw this element |
| `last_seen_sha` | git HEAD at the most recent ingest that confirmed it still exists |
| `invalid_sha` | git HEAD at the ingest that *removed* it (tombstone marker) |

**Behaviour change:** `delete_file()` no longer issues `DETACH DELETE`
when called from a git-aware ingest. Instead it stamps `invalid_sha` on
the File node, on every Symbol the file defines (excluding shared
`name::X` placeholders), and on every edge touching the File. The data
stays in the graph as a tombstone; topology queries filter it out by
default via a `WHERE invalid_sha IS NULL` predicate.

**Reason:** three concrete problems that pure HEAD-only graphs couldn't
answer cheaply:

1. *"This symbol got deleted in commit X — what used to call it?"*
   Pre-change: hard delete erased the answer. Re-ingesting at the
   parent SHA into a side project took 2+ hours on real codebases.
   Post-change: the symbol stays as a tombstone with `invalid_sha = X`,
   and `(caller)-[:CALLS]->(s)` with the tombstone filter relaxed
   answers in milliseconds.
2. *Drift detection.* "Is this comment / reference still accurate at
   HEAD?" Now answerable in one Cypher query: `MATCH (s:Symbol) WHERE
   s.last_seen_sha <> $head` returns everything the most recent ingest
   didn't confirm.
3. *Episode replay.* Each agent episode now stores the git HEAD it was
   reasoning over (see below). Combined with first/last SHA on graph
   elements, the graph state at episode time becomes
   reconstructable without rewinding the working tree.

**Migration:** legacy graphs (no temporal fields) keep working. The
upsert cypher uses `COALESCE(first_seen_sha, $head)` so the first
post-upgrade ingest backfills `first_seen_sha = last_seen_sha = current
HEAD` for every node it touches. The `WHERE invalid_sha IS NULL`
predicate is a no-op when the property is absent.

**Cost:** ~80 bytes of extra props per node + per edge. For the largest
indexed corpus (~200k symbols) that's ~16 MB. Storage grows monotonically
because tombstones aren't garbage-collected — a `code-memory vacuum
--before <sha>` command is on the roadmap.

**Not done in this slice:**
- No topological ordering on SHAs yet (would require `git rev-list
  --count` at ingest). Means "graph as of SHA X" queries still need an
  exact SHA, not a "before T" range.
- Edge-version tracking is coarse: an edge that disappears and reappears
  later collapses into one row whose `last_seen_sha` jumps over the gap.
  Fine for the use cases above; not enough for "show me every commit
  that toggled this call edge".

### Added — `Episode.head_sha`

**What:** the episodic store now persists the git HEAD active at the
moment the episode was recorded. New column on the SQLite `episodes`
table, idempotent ALTER migration for legacy databases.

**Reason:** without this field, episodes were timestamped wall-clock
floats with no link back to the code state the agent was reasoning
about. With it, episode replay can ask "what did the graph look like
when this patch was written?" by intersecting the episode's `head_sha`
with the new temporal stamps on graph elements.

**Reason it's optional / nullable:** non-git ingests still record
episodes; they just leave the field NULL. Legacy episodes recorded
before the migration also stay NULL, which means "unknown SHA" — not
"SHA was the empty string".

### Added — `FalkorStore.drift(head_sha)`

**What:** returns the list of Symbols whose `last_seen_sha` doesn't
match the supplied HEAD, classified as `tombstoned` (deleted) or
`drifted` (a prior incremental ingest missed it or it moved).

**Reason:** quickest possible answer to "did this codebase change in
ways my last incremental ingest didn't catch?" Useful for sanity-checking
a watcher that's been running for days and for finding stale references
in comments / docs.

### Anonymized internal corpus references

**What:** scrubbed private corporate identifiers (org name, internal
product slugs, absolute home-directory paths) out of `README.md`,
benchmark docs and JSON, plugin SKILL.md files, source comments, and
test fixtures. Replaced with neutral placeholders.

**Reason:** this repository is public. Original benchmark data mentioned
a private corporate codebase; leaving the names in would leak
employer-owned naming.

**Note for maintainers:** git history still contains the originals.
Anyone considering a clean-history rewrite (e.g. `git filter-repo`)
should treat this entry as the motivation.

### Added — Watch-root safety guard

**What:** `code-memory watch <path>` and the autostart bootstrap now
refuse to attach to filesystem roots, `$HOME`, `/tmp`, `/var`, `/etc`,
`/usr`, `/System`, `/Library`, `/opt`, `/Applications`, `C:/`,
`C:/Users`, `C:/Windows`, and `C:/Program Files`. The watcher emits
`error: refusing to watch …` and exits 2; the autostart adapter returns
a synthetic `<unsafe-root>` status without installing the unit.

**Reason:** a rogue `code-memory watch $HOME` had pinned 206% CPU and
accumulated 39 wall-hours of work walking every Node / IDE / browser
cache on disk. The launchd plist that registered it ran with
`KeepAlive=true` so `kill` alone wouldn't stop it. The guard makes the
configuration error impossible to commit by accident.

### Added — Ingest progress heartbeat

**What:** during `code-memory ingest` (full or incremental), a
`[code-memory] full ingest: files=N symbols=… chunks=… rate=X/s` line
goes to **stderr** every 50 files (configurable via
`CODEMEMORY_PROGRESS_EVERY`, disable with `CODEMEMORY_PROGRESS=0`).
Stdout / `--json` output is unchanged.

**Reason:** large ingests (133K chunks against bge-m3 via Ollama)
take 2+ hours, and the CLI used to print nothing until the very end.
Users assumed the process had hung and killed it, losing all in-progress
work. The heartbeat is the cheapest possible "yes it's still running"
signal that doesn't pollute the JSON contract.

### Added — .NET ecosystem language support

**What:** the tree-sitter extractor now recognises:

- `.cs` (C#)
- `.razor`, `.cshtml` (Razor — embeds C# inside HTML)
- `.vb` (VB.NET)
- `.fs`, `.fsi`, `.fsx` (F#)

For each, the symbol / import / call node-type set is extended so
classes, methods, properties, namespaces, modules, `using` / `Imports` /
`open` directives, and method invocations are correctly extracted. F#
call edges are intentionally skipped: `application_expression` is too
recursive to produce useful graph edges (`x + y` would parse as a call
to `op_Addition`). Default-ignored directories now include `bin`, `obj`,
`packages`, `TestResults`, `.vs`, `artifacts`.

**Reason:** the project previously parsed only TS / JS / Python. Real
mixed-stack repositories (Angular front + .NET back) had a blind spot
that ranked retrieval purely on dense vector similarity without graph
topology for half the codebase. With this, callers / callees /
definitions all work across the .NET half — except for the cross-file
resolver, which still doesn't understand C# namespaces (it only
handles relative imports). That gap is tracked as a follow-up.
