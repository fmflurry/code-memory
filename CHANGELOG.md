# Changelog

High-level notes on what changed and **why**. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with an extra
"Reason" line per entry because the *why* matters more than the *what*
when the repo grows.

This file complements `git log`: commits explain mechanics, this file
explains intent.

## [0.5.2] — 2026-05-30

Release theme: **the watcher stops leaking**. A diagnosis of runaway RAM
turned up dozens of orphaned `code-memory watch` daemons — one permanent
launchd agent per directory any MCP server had ever booted in, including
throwaway per-session dirs. This release stops the bleed and self-heals
the cruft. Also lands semantic dedupe for near-duplicate user claims.

### Fixed — Unbounded launchd watcher accumulation

**What:** `ensure_autostart()` no longer registers a persistent OS agent
for ephemeral / per-session directories (`~/.claude/homunculus/*`,
`.cursor/worktrees/*`, `plugins/cache/*`); the session-scoped in-process
watcher still covers them. `LaunchdAdapter.prune_stale()` (run on every
MCP bootstrap) boots out and removes agents whose `WorkingDirectory` is
gone or ephemeral.

**Reason:** each MCP boot registered a `KeepAlive` + `RunAtLoad` agent for
its cwd. Ephemeral session dirs got permanent watchers that survived kill
(launchd relaunched them) and reboot, piling up dozens of daemons and the
RAM/CPU they pin. `prune_stale` is launchd-only for now; systemd/schtasks
GC is a follow-up.

### Fixed — Graceful watcher teardown on MCP shutdown

**What:** the MCP server registers `atexit` + `SIGTERM` handlers that call
`Watcher.stop()`.

**Reason:** the in-process watcher ran as a daemon thread with no clean
stop, so an in-flight debounced sync was dropped and the watchdog Observer
never joined cleanly on exit.

### Added — Semantic dedupe of near-duplicate claims

**What:** `ClaimsIndexer` collapses a new claim into the closest open claim
when cosine similarity is `>= CLAIMS_SEMANTIC_DEDUP_THRESHOLD` (default
0.90), ahead of the SQL write.

**Reason:** exact (subject, predicate, object) dedupe missed paraphrases
("project uses flurryx" / "project depends-on flurryx"), so semantically
identical claims were stored three times. Best-effort — embedder/backend
errors fall through to the existing SQL-level dedupe; set `>= 1.0` to
disable.

## [0.3.0] — 2026-05-26

Release theme: **the claim loop closes**. v0.2.0 shipped the storage
side of user claims (bi-temporal SQLite + Graphiti-style extraction).
This release ships everything around it — detection at prompt time,
agent-authored assertion via MCP, dedupe at write time, plus a hard
fix for the silent-hook bug that meant the whole feature was off by
default. Also lands the Angular clean-arch resolution work: TS
abstract classes + receiver-type inference + DI graph queries.

### Added — Plugin claim-intent detection (Claude Code + OpenCode)

**What:** every user prompt is scanned for durable-assertion patterns
(preferences, decisions, rejections, ownership, location). When a
match fires, both plugins inject a polarity-flipped nudge into the
agent's system prompt **before** the agent sees the message:

```
[code-memory] Durable user assertion detected — ACT BEFORE ANSWERING.
…
DEFAULT ACTION: call codememory_assert_claim NOW, in the same response,
BEFORE any other tool call or user-facing text.
…
DO NOT skip because: the fact is already in CLAUDE.md / the wording is
emotional / the user is also asking a question / you are not sure of
the scope.
SKIP ONLY if ALL of these hold: hypothetical, counterfactual, retracted,
or a higher-confidence dupe was asserted in this session.
If you skip, state ONE LINE: "skipped claim: <reason>". Silent skips
are a bug.
```

- Claude Code: `plugins/claude-code/scripts/lib/claim-intent.js` +
  injection via `UserPromptSubmit` → `additionalContext`.
- OpenCode: `plugins/opencode/src/code-memory-lib/claim-intent.ts` +
  injection via `chat.message` → `experimental.chat.system.transform`.
- Patterns and nudge template are kept lockstep across the two plugins;
  21 CC tests + 18 OpenCode tests pin the regex behavior, the pure-
  question filter, and the formatted template text.

**Reason:** the v0.2.0 extractor only fired post-turn from `session.idle`
via Ollama, so a user typing "I love Clean Architecture" produced no
claim if the agent didn't think to call `codememory_assert_claim`
itself. Polarity-flipped wording + explicit anti-rationalizations
move the default from "skip" to "assert", and the mandatory "skipped
claim: <reason>" line makes silent drops detectable in the transcript.

### Added — Agent-authored claims via MCP

**What:** `codememory_assert_claim(subject, predicate, object, project, …)`
MCP tool lets the agent register a claim directly without invoking the
extractor LLM. Predicate is canonicalized to lowercase-kebab so
single-valued-predicate contradiction handling (e.g. "uses → switches
DB") still works. Bypasses the `CLAIMS_EXTRACTION` flag (no Ollama
in the loop). Entity resolution runs if the resolver is wired up,
otherwise the row stores raw strings.

**Reason:** matches the plugin nudge above. The agent now has a
first-party tool to call when the nudge fires; the extractor remains
available for cases where claims need to be pulled out of historical
prompts post-hoc.

### Added — Claims store dedupe at write time

**What:** `ClaimsStore.upsert` looks for an open row matching
`(subject, predicate, object, polarity)` and refreshes it in place
instead of inserting a duplicate:

- `confidence` is monotonic non-decreasing (`MAX(prev, new)`).
- Non-empty `evidence_span` overwrites a blank one; never the reverse.
- `recorded_at` advances.
- `session_id`, `source_prompt_id`, `entity_*` keep the FIRST
  observation's provenance via `COALESCE`.
- Polarity flips and different-object assertions still go through the
  bi-temporal close path (`valid_to` set on the prior row).

**Reason:** the new "ACT BEFORE ANSWERING" nudge caused the agent to
re-assert the same claim every turn it qualified, bloating `claims.db`
with identical rows. Dedupe at write keeps the audit trail useful
without losing the bi-temporal semantics — `current()` still returns
exactly one row per (s,p,o,polarity), `as_of()` queries still see the
right history.

### Fixed — Claude Code plugin install actually enables the plugin

**What:** the previous `plugins/claude-code/install.sh` only symlinked
the repo into `~/.claude/plugins/code-memory/`. Claude Code ignores
that path — its plugin loader reads only
`~/.claude/plugins/installed_plugins.json` (marketplace-installed
plugins). Result: `hooks.json` was on disk but no hooks fired. The
hooks panel in Claude Code correctly reported "No hooks configured for
UserPromptSubmit" — the user just had no way to know symlinking
wasn't enough.

The fix:

- New marketplace manifest at `.claude-plugin/marketplace.json` at the
  repo root, pointing at `./plugins/claude-code`.
- `install.sh` rewritten: validates both manifests, runs
  `claude plugin marketplace add <repo>`, runs
  `claude plugin install code-memory@code-memory`, then registers the
  MCP server as before. Idempotent (uninstall+reinstall on re-run).
- `plugins/claude-code/README.md` updated; main README documents the
  marketplace step and warns against bare-symlink installs.

**Reason:** the whole plugin was off by default for every user who
followed the documented install. Plugins not registered with the CC
loader are inert no matter what's on disk.

### Added — TypeScript abstract class + receiver-type inference

**What:** the tree-sitter extractor now recognizes
`abstract_class_declaration` / `abstract_method_signature` as symbol
nodes, and `Call` carries an optional `receiver_type` field inferred
from `this.<field>.<method>()` where the field's type is readable
from a member initializer or annotation.

**Reason:** Angular clean-arch use cases call their port via
`this.port.method()` where `port` is `inject(SomeAbstractPort)`. Without
abstract-class recognition the port never enters the graph; without
receiver-type inference the resolver can't narrow `method()` to the
port's overloads, so every Angular use case looks like an orphan call.
The pattern is widespread enough in modern Angular codebases that
losing it bricks topology queries on the most common architectural
shape.

### Added — DI graph queries (`codememory_injects`, `codememory_injectors`)

**What:** two new MCP tools matching the existing `callers` / `callees`
shape, but operating on DI injection edges:

- `codememory_injects(symbol)` — what tokens does this file inject?
- `codememory_injectors(token)` — which files inject this token?

**Reason:** in clean-arch / hexagonal projects the DI graph carries
nearly as much architectural signal as the call graph. Surfacing it as
a first-class topology query closes one of the gaps the v0.2.0
codebase-exploration rule called out.

### Added — Layered config via `.code-memoryrc`

**What:** `code_memory.config` now reads `KEY=VALUE` overrides from
`./.code-memoryrc` (project-local) and `~/.config/code-memory/config`
(global). Real shell env wins; project file beats global file.

**Reason:** users were embedding env exports into shell rc files just
to pin one default per project (e.g. `CODE_MEMORY_PROJECT=auto`).
Layered config files keep those defaults out of the shell environment
and make per-project overrides commitable when desired.

### Added — Tests

- `tests/test_extractor_ts_abstract.py` — abstract-class symbol pickup.
- `tests/test_extractor_ts_inject.py` — `inject()` call extraction.
- `tests/test_extractor_receiver_type.py` — receiver-type inference.
- `tests/test_mcp_assert_claim.py` — MCP tool happy path + invalid
  inputs + canonicalization + entity resolution.
- `tests/test_claim_store.py` — 5 new dedupe tests covering same-fact
  reupsert, max-confidence, non-empty-evidence wins, first-session-id
  preservation, polarity-differs-not-dedupe.

Total: **496 passed, 3 skipped** (Python), **21 CC + 18 OpenCode**
plugin tests pass.



Release theme: **enterprise-grade ingest, full features, no quality
tradeoffs**. The bulk of this release attacks the cold-ingest problem
on large repos and the precision gaps in topology queries. Honest
framing throughout — every speedup keeps `bge-m3` recall intact;
every limit is documented.

### Added — `Snapshots: ingest once, sync everywhere`

**What:** an end-to-end fix + headline workflow for the cold-ingest
problem.

- `code-memory snapshot publish` builds a tar.gz containing every
  Qdrant vector (with hybrid dense + sparse), every Falkor node and
  edge, the ingest state pointer, and a SHA-256-digested manifest;
  pushes it to a dedicated git branch (default
  `codemem-snapshots`). Distribution channel is just git.
- `code-memory sync` after a `git pull` auto-detects whether a
  snapshot exists for HEAD, a recent ancestor, or neither — and
  pulls + applies the snapshot, or applies the ancestor + runs the
  delta, or falls back to a full ingest. Verified end-to-end:
  cold-ingest → publish → wipe → sync restores `retrieve` /
  `callers` / `definitions` / `importers` in 0.5 s (snapshot
  matches HEAD) or 0.9 s (HEAD one commit ahead).

**Reason:** `bge-m3` on Apple Silicon Metal is hardware-bound at
~21 chunks/sec — a 17k-file monorepo takes ~2 h cold no matter how
clever the pipeline gets. The real fix is to never run the embedder
on slow machines: build the index once on a fast host (CI / GPU box),
publish, sync everywhere else. The cold-ingest cost moves from
"every dev on every clone" to "once per merge in CI".

### Fixed — Snapshot hybrid-vector round-trip

**What:** `_dump_vectors` used `list(p.vector)` which silently returns
the dict's keys when Qdrant returns the hybrid layout. Snapshots
looked valid (right manifest, right counts) but every vector blob
carried the literal string `["dense"]` instead of the embedding.
Apply then crashed in `QdrantStore.upsert` accessing `vector.dense`
on a list of slot names.

New `_normalize_vector_for_dump(vec)` coerces both the hybrid dict
and legacy bare-list layouts into one canonical JSON shape; new
`_hybridvec_from_dump(payload)` is the reverse on apply. Backward
compat: snapshots written with the bare-list shape still load.

**Reason:** the snapshot feature was non-functional in shipping code.
Format-only tests round-tripped JSON correctly, but no test
exercised the actual Qdrant → snapshot → Qdrant path. Added 8 new
tests in `tests/test_snapshot_e2e.py` (6 pure-format, 2 live against
real Falkor + Qdrant) to keep the regression dead.

### Added — `REFERENCES` edge type for C# type-position usage

**What:** the graph extractor now emits a new `REFERENCES` edge for
every type-position usage of a symbol — base lists (`class X : IFoo`),
parameter types, field/property types, generic args, type
constraints, cast / is / as / typeof. `callers` queries union
`CALLS | REFERENCES` so an interface like `IFooService` returns both
the call sites of its members and the files that declare it as a
dependency.

**Reason:** `code-memory callers IFoo` on a C# repo used to return
0 because the graph only modeled call expressions. Implementations,
parameter-type users, and generic-arg sites were invisible — the
single biggest precision gap on .NET corpora. Verified end-to-end
on a fresh C# project: `callers IDocumentServiceBase` (renamed
to `IFooService` in tests) now surfaces the impl class **and**
every consumer that declares the interface as a constructor or
field type.

### Added — Canonical import aliasing for Python relative imports

**What:** two fixes that close the import-precision gap.

1. Extractor: `_import_module` now reads the `module_name` field on
   `import_from_statement` instead of the first matching `dotted_name`
   child. Without this, `from ..pkg.sub import Sym` stored `Sym`
   (the imported name) as the module key — every relative import in
   the project was filed under the wrong slot.
2. Resolver: for each relative import that resolves to a project
   file, derive the canonical dotted-module name by climbing
   `__init__.py` parents and emit alias `IMPORTS` edges. Also taught
   `_resolve_relative_import` to handle Python dotted-relative form
   (`..pkg.sub.leaf`), not just TS/JS path-style. New
   `import_aliases_added` stat on `ResolverStats`.

**Reason:** before this, `code-memory importers code_memory.graph.falkor_store`
on this very repo returned 2 (only the test files using the absolute
form) when the actual answer was 6 (4 source files use relative
imports). After the fix: 6 true positives, 0 false positives —
strictly more precise than `rg`'s lexical match.

### Added — Persistent content-hash embedding cache

**What:** new `EmbedCache` (SQLite-backed) and `CachedEmbedder`
wrapper. Every chunk's UTF-8 SHA-256 + the embedding model id become
the cache key. The wrapper hashes inputs, fetches cached hits in one
`SELECT … WHERE chunk_hash IN (…)`, sends only the miss list to
the inner backend, writes new vectors back, reassembles in input
order. Lives in `$DATA_DIR/embed_cache.sqlite`; shared across
projects (content hashes are content-only). Disable via
`EMBED_CACHE_DISABLED=1`.

**Reason:** on a stable monorepo, ~95% of chunks are unchanged from
one ingest to the next. The cache lets daily re-ingest collapse to
a SQLite scan + Qdrant upsert. Measured on this repo: 55.7 s cold
→ **2.9 s warm = 19× faster** on re-ingest, full features and
identical answers.

### Added — TEI (`text-embeddings-inference`) embedding backend

**What:** new `TEIEmbedder` class + `EMBED_BACKEND=tei` /
`TEI_URL` config. The optional Docker compose profile
`docker compose --profile tei up -d` launches a TEI container
alongside FalkorDB and Qdrant. Cache key is shared with Ollama
when both serve the same model, so switching backends doesn't
re-embed.

**Reason:** on Linux + NVIDIA, TEI serves `bge-m3` at 5-10× the
throughput of Ollama via proper CUDA batching + streaming.
Cold-ingest of a 17k-file monorepo drops from ~2 h (Ollama on Mac
Metal) to ~15-25 min (TEI on Linux). Same `bge-m3` weights, zero
recall change. The Mac CPU image works as a smoke test but offers
no advantage over Ollama's Metal path.

### Added — `--no-vectors` ingest flag

**What:** `code-memory ingest --no-vectors` skips the embedder and
Qdrant entirely, building only the symbol graph. `callers` /
`definitions` / `importers` answer identically; semantic `retrieve`
returns empty.

**Reason:** documented as a niche option for agents that only
consume the graph layer — not a recommendation. The cache above
solves the slow-ingest problem with full features; this flag is
for the genuinely-no-semantic-recall use case.

### Performance — UNWIND-batched Falkor upserts

**What:** `FalkorStore.upsert_nodes` / `upsert_edges` previously
ran one `MERGE` query per node / edge. A file with 50 symbols +
imports + calls hit Falkor 50 times. Now they group by label
(nodes) and `(src_label, type, dst_label)` (edges) and ship one
`UNWIND $rows` per group: ~50 round-trips → ~3 per file.

**Reason:** ~10× faster graph layer, especially on import-heavy
languages. Temporal stamping preserved bit-for-bit.

### Performance — Cross-file embedding batches + pipelined Qdrant

**What:** `_ingest_full` now buffers chunks across files and
flushes to the embedder in batches of 64 (per-file Ollama HTTP
overhead was ~75 ms each before). Qdrant upserts run on a bounded
2-worker thread pool so upload of batch N overlaps with embed of
batch N+1.

**Reason:** per-file HTTP overhead was the second-biggest cost
after model inference. Modest on Mac (Ollama serialises on GPU);
meaningful on Linux + NVIDIA where the embedder is faster than
the qdrant network path.

### Added — Auto-resolved `embed_dim` from model name

**What:** new `_KNOWN_MODEL_DIMS` table in `config.py` and
`resolve_embed_dim(model_name, override)` helper. `Config.embed_dim`
defaults to `0` (sentinel for "auto"); `QdrantStore.__init__`
resolves the actual dim from the model name unless `EMBED_DIM=<n>`
is set explicitly.

**Reason:** swapping `EMBED_MODEL` without setting `EMBED_DIM`
silently truncated or rejected upserts. The known-model table
covers bge, mxbai, and snowflake-arctic at standard sizes;
unknown models default to 1024 with a stderr warning.

### Docs — README headline + framing pass

**What:** the README leads with the verified topology benchmark
table (this repo, Python files, `FalkorStore`): code-memory beats
`rg` on precision (6 true importers vs rg's 7 with 1 false
positive) while returning 5-30× less context for the agent to read
back. The Performance & scale section documents the three real
unlock paths (snapshots, cache, TEI) and an explicit "Honest
limits today" subsection naming what isn't and won't be on the
roadmap.

Earlier "47× faster!" framing tied to `--no-vectors` was walked
back as misleading. Nomic-embed-text was evaluated against
`bge-m3` on a real corpus, lost 42% Recall@10, and removed from
recommendations.

### Removed — Private app names from current tree and full git history

**What:** scrubbed `gc.net`, `gc.webapp`, `isagri`,
`GC.BillingChain`, `IDocumentServiceBase` and related variants
from docs, scripts, tests, and the `BENCHMARK_VS_BASELINE.json`
(443 path leaks). Ran `git filter-repo` to rewrite all 53 commits
of history; force-pushed `main`.

**Reason:** the names belong to private professional projects of
the author's. Anyone with an existing clone needs to delete and
re-clone — SHAs are orphaned. The backup branch
`main-backup-pre-scrub-20260525-142651` is kept locally as a
safety net.

### Added — Claim entity resolution + retrieve-pack surfacing + OpenCode parity

**What:** three follow-ups to the Graphiti-style claim layer:

- `claims/resolver.py` — `EntityResolver` embeds each claim's subject
  and object via the project's Ollama embedder, searches a new
  per-project Qdrant collection `claim_entities__<slug>`, and reuses
  the top hit when cosine ≥ `CLAIMS_ENTITY_THRESHOLD` (default `0.85`).
  Reused entities accumulate surface forms in a payload `aliases` list;
  unmatched ones mint a fresh UUID. Two new nullable columns on the
  `claims` table (`entity_subject_id`, `entity_object_id`) are added
  via idempotent migrations — old DBs upgrade in place.
- `Retriever.retrieve` now pulls top-K claims by query-token overlap
  with mild recency decay (30-day half-life) and surfaces them inside
  the `ContextPack`. The pack's `render()` / `to_dict()` outputs gain a
  `claims` section; the Claude Code and OpenCode plugins both render
  the new section in their auto-injected Context Pack. No-ops cleanly
  when the project has no `claims.db`.
- OpenCode plugin: `extractClaimsDetached` on the `MemoryClient`
  interface, wired into the existing `session.idle` event so claim
  extraction parity with the Claude Code plugin is automatic. `ClaimHit`
  type added to `code-memory-lib/memory-client.ts`.

**Reason:** without resolution, two surface forms of the same entity
("Postgres", "postgres", "Postgres DB") would create three rows that
look independent — contradiction handling and downstream aggregation
would silently miss the connection. Qdrant + Ollama are already in the
stack; reusing them keeps the infra footprint flat. Surfacing claims
in retrieve packs closes the read loop: facts you've told the agent
once now resurface automatically in the next relevant turn, without
the agent having to know they exist. The OpenCode mirror just keeps
the two plugins in lockstep so behavior doesn't diverge by host.

15 new tests cover resolver thresholds, alias accumulation, embedder /
Qdrant failure fallback, and claim ranking + surfacing in the
ContextPack.

### Added — Graphiti-style user-claim extraction

**What:** a new `src/code_memory/claims/` subpackage that turns
substantive user prompts into structured `(subject, predicate, object)`
claims with bi-temporal validity (`valid_at`, `valid_to`,
`recorded_at`, `head_sha`). Pieces:

- `claims.extractor.ClaimExtractor` — thin Ollama `/api/chat` client
  with JSON-schema-constrained output (default model `gemma2:9b`),
  literal-substring `evidence_span` anti-hallucination guard,
  configurable `min_confidence` filter, and case-insensitive dedup.
- `claims.store.ClaimsStore` — SQLite store mirroring the episodic
  store's idempotent-migration pattern. Single-valued predicate
  registry (`uses`, `prefers`, `deployed-to`, `owns`, `is-located-at`,
  `is-a`, `assigned-to`, `depends-on`) auto-closes prior conflicting
  assertions on upsert. Read paths: `current()`, `as_of(when)`,
  `by_id()`.
- New CLI subcommand `code-memory extract-claims --prompt "..."` plus
  detached `extractClaimsDetached` helper in the Claude Code plugin so
  the Stop hook fires extraction without blocking session end.
- New MCP tools `codememory_extract_claims` and `codememory_claims` so
  agents (and the OpenCode plugin) can drive extraction and read claims
  through the same surface as everything else.
- `Config` now carries `claims_enabled`, `claims_llm_model`,
  `claims_llm_timeout`, `claims_min_confidence`, and a per-project
  `claims_db` path under `data/<slug>/claims.db`.
- `scripts/install.sh` gains a `--with-claims` flag and an interactive
  prompt to pull `gemma2:9b` (~5.4 GB) alongside `bge-m3`.

**Reason:** episodes store the whole prompt as opaque text, which means
"what did the user say about X last Tuesday?" requires re-reading every
prompt. Graphiti showed that a bi-temporal claim graph closes that gap
cheaply. Three constraints kept us from copy-pasting their stack:

1. **No cloud LLM.** We already require Ollama for embeddings — adding
   a chat model on the same daemon is one `ollama pull` away. Local
   keeps prompts off the network.
2. **No new database.** SQLite is already in the stack for episodes;
   bi-temporal columns are just four extra fields. The graph store
   (FalkorDB) stays focused on code topology.
3. **No latency budget on the hot path.** Extraction is detached from
   the on-stop hook and only runs when `CLAIMS_EXTRACTION=true`, so a
   default install never pays the inference cost.

Disabled by default. Opt in with `CLAIMS_EXTRACTION=true` after
`ollama pull gemma2:9b`.

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
