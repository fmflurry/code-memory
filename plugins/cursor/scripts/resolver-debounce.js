#!/usr/bin/env node
/**
 * Debounced resolver worker.
 *
 * Spawned detached from PostToolUse with the cwd as argv[2]. Behavior:
 *
 *   1. Record the marker timestamp observed at spawn.
 *   2. Sleep RESOLVER_DEBOUNCE_MS.
 *   3. Re-read the marker. If a newer PostToolUse hook has touched it
 *      since we slept, another worker is now in flight — exit and let
 *      that one win.
 *   4. Otherwise, fire `code-memory resolve --json` (detached). The
 *      resolver re-points placeholder `name::X` CALLS edges to real
 *      Symbol nodes across the whole graph.
 *
 * Multiple workers can be spawned by a burst of writes; only the last
 * one will see a stable marker and actually run resolve. This is the
 * Claude Code equivalent of the JS-side setTimeout debounce used in
 * the OpenCode plugin.
 */

const { createMemoryClient } = require("./lib/memory");
const { readResolverMarker } = require("./lib/state");

const RESOLVER_DEBOUNCE_MS = 1500;

(async () => {
  const cwd = process.argv[2] || process.cwd();
  const observed = readResolverMarker(cwd);

  await new Promise((resolve) => setTimeout(resolve, RESOLVER_DEBOUNCE_MS));

  const current = readResolverMarker(cwd);
  if (current !== observed) {
    // A newer worker is scheduled; let it run resolve.
    process.exit(0);
  }

  const mem = await createMemoryClient({ cwd, log: () => {} });
  if (!mem.available) process.exit(0);
  mem.resolveDetached();
  // Give spawn() a tick before we exit so the child detaches cleanly.
  setTimeout(() => process.exit(0), 50);
})();
