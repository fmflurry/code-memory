# Changelog

High-level notes on what changed and **why**. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with an extra
"Reason" line per entry because the *why* matters more than the *what*
when the repo grows.

This file complements `git log`: commits explain mechanics, this file
explains intent.

## Unreleased

### Fixed — Pipeline now actually writes temporal stamps

**What:** every call site in `orchestrator/pipeline.py` that touches
`upsert_nodes` / `upsert_edges` / `delete_file` now forwards
`head_sha` and `head_ord`. A new module-level `_resolve_head(root)`
resolves the git HEAD + first-parent ordinal once per ingest and
threads them through `_ingest_full`, `_ingest_delta`, `ingest_file`,
`reingest_file`, `_upsert_graph`, and all four `_index_*` helpers
(csproj projects, referenced assemblies, file containment, .sln
solutions). `reingest_file` auto-resolves head from the file's
enclosing repo when the caller (e.g. a save-file hook) doesn't
supply one.

**Reason:** the storage-layer temporal work shipped in a separate
slice and the pipeline integration regressed during the .NET trust
pass merge — the `head_sha=` kwarg got dropped from every upsert
call site. `FalkorStore.upsert_nodes` treats `head_sha=None` as
"skip stamping" (intentional legacy fallback), so every ingest
silently wrote zero `first_seen_sha` / `last_seen_sha` values. The
`at_sha` / `drift` / `callers_at_sha` MCP tools queried a graph that
had no temporal data to query and returned empty by design.

Verified empirically: a freshly ingested project on the fixed
pipeline now writes stamps on every File, Symbol, CALLS, IMPORTS,
DEFINES, CONTAINED_IN, USES_ASSEMBLY, EXPOSES_TYPE, MEMBER_OF, and
INJECTS edge.

**Reason for the auto-resolve in `reingest_file`:** the Claude Code
/ OpenCode plugins fire `code-memory reingest <path>` per save hook
without computing the SHA themselves. A single `git rev-parse HEAD`
per hook call is cheap enough that requiring callers to pass it
would just produce empty stamps in practice. Non-git directories
fall through cleanly (`(None, None)`) and keep the legacy unstamped
path.

### Added — .NET production-grade trust pass

**What:** four layers built on top of the basic .NET language
support, in order of dependency:

1. **Ingest-time sanity check.** Every plain-identifier Symbol's
   snippet must contain its own name as a whole word
   (`extractor/sanity.py`). The check uses regex word-boundary
   matching, not bare substring, so a truncated `mmandeRules` no
   longer slips through inside a real `CommandeRules` snippet.
   Failure rate lands on `IngestStats.sanity` with sample
   violations; rates above 2% append a loud `notes` entry so the
   ingest stops being silently wrong.

2. **Partial class merge.** `partial class` / `struct` / `interface`
   / `record` across N files now collapse to a single graph node
   keyed `partial::{namespace}.{Name}` with N `DEFINES` edges from
   each contributing file. Detection covers block-scoped and C# 10
   file-scoped namespaces.

3. **Project topology** — `extractor/csproj.py` parses SDK-style
   and legacy `.csproj` / `.fsproj` / `.vbproj`. Pipeline emits
   `Project` nodes, `PROJECT_REFERENCES` (Project → Project), and
   `PACKAGE_REFERENCES` (Project → Package). Unparseable XML and
   refs pointing outside the repo are dropped — no dead nodes.

4. **Assembly metadata** — `extractor/dll.py` (pure-Python via
   `dnfile`, no .NET runtime needed) reads PE/CLR metadata. Pipeline
   resolves `<PackageReference>` against the NuGet global cache via
   `extractor/nuget.py` (TFM fallback chain net8.0 → net6.0 →
   netstandard2.1 → …) plus project `bin/{config}/{tfm}` outputs,
   and emits `Assembly` + public `Type` nodes with `USES_ASSEMBLY`
   (Project → Assembly) and `EXPOSES_TYPE` (Assembly → Type) edges.

5. **File → Project containment.** Every .NET source file gets a
   `CONTAINED_IN` edge to the deepest `.csproj` whose directory
   prefixes the file path. Files outside any csproj stay unowned
   rather than getting misattributed.

6. **Cross-assembly call resolution.** The resolver gained a fifth
   tier: a call name whose owning project references an `Assembly`
   that uniquely exposes a `Type` with that name becomes a resolved
   `(:File)-[:CALLS]->(:Type)` edge with `confidence="external"`
   and a `via_assembly` property. Ambiguous matches (multiple
   referenced assemblies exposing the same name) stay unresolved by
   design — no coin-flipping.

7. **Solution grouping.** `extractor/sln.py` parses `.sln` files
   (SDK-era + legacy with xmlns). Pipeline emits `Solution` nodes
   and `MEMBER_OF` edges (Project → Solution). Solution-folder
   pseudo-entries (type GUID `2150E333…`) are dropped.

8. **Razor `@inject` DI graph.** `.razor` / `.cshtml` `@inject`
   directives emit `INJECTS` edges from the view file to the
   injected interface or class. The resolver runs the same four-tier
   resolution on `INJECTS` as it does on `CALLS`, so an
   `@inject IUserService` in a Razor view resolves to either the
   in-project interface or a `Type` from a referenced assembly.

9. **Member-level DLL access on demand.** New MCP tool
   `codememory_assembly_members(type, project, assembly?)` reads
   public methods (with parameter counts) directly from the DLL at
   query time. Members aren't bulk-indexed because a single NuGet
   package can expose 10k+ members; on-demand keeps the graph small
   while still answering "what's on this type" for overload
   disambiguation.

10. **Overload disambiguation by call-site arity.** `Symbol` gains
    `param_count`; every `CALLS` edge carries an `args` arity. The
    resolver's project-unique and imported tiers gained an arity
    tiebreak — when N candidates share a name, pick the one whose
    `params` matches the call's `args`. Mismatch or tie stays
    ambiguous.

**Reason:** before this pass, the .NET grade was a generous C on
core features and F on cross-assembly resolution / DI / solution
grouping / member access / overloads. A coding agent asking "who
calls `JsonConvert.SerializeObject`" got nothing because the call
landed on an orphan placeholder. After this pass, the graph
answers cross-assembly with attribution, .NET 10's file-scoped
namespaces parse cleanly, partial classes show as one logical
entity, and the agent can list type members without ballooning
graph size.

### Fixed — UTF-8 byte/char offset chop in tree-sitter slicing

**What:** `extractor/treesitter.py` now reads source as bytes,
strips a UTF-8 BOM if present, parses bytes through tree-sitter,
slices bytes via `_slice(source: bytes, node)`, and decodes UTF-8
only at the very end. Every helper (`_symbol_name`, `_callee_name`,
`_import_module`, `_first_identifier_deep`) takes bytes.

**Reason:** the old code passed bytes to tree-sitter but indexed a
Python `str` with the byte offsets tree-sitter returned. For any
file with non-ASCII content above a symbol (French comments,
identifiers with accents, German Python docstrings) the offsets
drift once and stay drifted — every subsequent identifier was
chopped from the front. Real example caught on a French C# repo:
`class_declaration -> "mmandeRules<T"` instead of `CommandeRules`,
`imports -> "stem;\\n"` instead of `System`, every callee truncated
to a noise prefix. The graph stored those garbled names, so
queries for `CommandeRules` or `DocumentService.Sauver` returned
nothing.

Symptom severity scaled with the volume of non-ASCII content above
each symbol; English codebases sometimes appeared fine for hundreds
of symbols before failing on the first file with an accented
comment. The bug was invisible until someone tried to use the graph.

### Added — BGE-M3 hybrid embed backend (opt-in)

**What:** `code_memory.embed` now supports two backends behind a
shared `HybridVec` (dense + sparse) shape:

- **Ollama** (default) — dense-only via the Ollama daemon. Stays
  warm across short-lived processes (per-save reingest hooks, git
  hooks) so cold-load doesn't tax the user.
- **FlagEmbedding** (`EMBED_BACKEND=flagembed`, requires
  `[hybrid]` extra) — in-process BGE-M3 producing dense + sparse
  from one forward pass. Heavy (~2.3 GB weights, ~5-15 s cold load)
  but enables true hybrid retrieval.

`QdrantStore` collections use named-vector layout (`dense` +
`sparse` slots) with IDF modifier on the sparse side, so flipping
between backends doesn't require schema changes. Search supports
`mode={dense, hybrid, hybrid_dbsf}`; hybrid falls through to dense
when the query vector has no sparse component (Ollama path).

Hybrid retrieval is **opt-in** via `CODEMEMORY_HYBRID=1`. Benchmarks
on a 2.6k-file Angular corpus showed pure dense outperforming
hybrid (RRF and DBSF) on natural-language queries — m3's dense
head is strong enough that adding sparse tends to surface
generated API stubs and `.spec.ts` files that share identifier
vocabulary with the query without bringing new wins. The opt-in
exists for symbol-heavy corpora; re-run `scripts/benchmark.py`
before flipping.

**Reason:** Ollama by default keeps per-file save-hook reingests
viable — the alternative (in-process model load per CLI
invocation) added 5-15 s startup to every Write/Edit. Keeping the
collection layout hybrid-ready means users can later opt in to the
sparse signal without re-ingesting.

### Added — Retrieval benchmark harness

**What:** `scripts/benchmark.py` runs a fixed query set through six
modes (dense, hybrid RRF, hybrid DBSF, each with and without
cross-encoder rerank) against a populated Qdrant collection and
reports Recall@5 / Recall@10 / MRR / nDCG@10 / p50 / p95 latency.
Output is markdown (`docs/BENCHMARK.md`) + raw JSON
(`docs/benchmark-raw.json`). 30 hand-crafted gold queries against
a sample Angular corpus ship as a baseline.

**Reason:** retrieval-quality regressions were invisible until an
agent complained. A reproducible benchmark lets future model /
chunk-text / fusion changes be evaluated against the same gold set
before they ship.

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
