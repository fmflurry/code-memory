#!/usr/bin/env node
/**
 * afterFileEdit hook.
 *
 * Two jobs:
 *   1. `code-memory reingest <file_path>` — fire-and-forget. Tree-sitter
 *      re-parses the file; its symbol nodes + edges + Qdrant chunks are
 *      replaced.
 *   2. Schedule the cross-file resolver via a debounced detached worker
 *      (resolver-debounce.js). A burst of edits collapses to exactly one
 *      resolver run, ~1.5 s after the last write.
 *
 * Output is ignored by Cursor for this hook; we just exit.
 */

const path = require("node:path");
const { spawn } = require("node:child_process");
const { readEvent, done } = require("./lib/io");
const { createMemoryClient } = require("./lib/memory");
const { touchResolverMarker } = require("./lib/state");

(async () => {
  const ev = await readEvent();
  const filePath = String(ev.file_path || "");
  if (!filePath) {
    done();
    return;
  }

  const cwd =
    (Array.isArray(ev.workspace_roots) && ev.workspace_roots[0]) ||
    process.env.CURSOR_PROJECT_DIR ||
    process.cwd();

  // Guard: only reingest files that live inside the project root (cwd).
  // Resolving against cwd handles relative paths; the sep-suffix check
  // prevents false positives like /foo/bar matching the prefix of /foo/baz.
  const projectRoot = path.resolve(cwd);
  const absFilePath = path.resolve(cwd, filePath);
  if (absFilePath !== projectRoot && !absFilePath.startsWith(projectRoot + path.sep)) {
    // File is outside the project — silently skip ingestion.
    done();
    return;
  }

  const mem = await createMemoryClient({ cwd, log: () => {} });
  if (!mem.available) {
    done();
    return;
  }

  mem.reingestDetached(filePath);

  touchResolverMarker(cwd);
  try {
    const worker = path.join(__dirname, "resolver-debounce.js");
    const child = spawn(process.execPath, [worker, cwd], {
      detached: true,
      stdio: "ignore",
      env: process.env,
    });
    child.unref();
  } catch {
    // ignore — resolver will run on the next opportunity
  }

  done();
})();
