#!/usr/bin/env node
/**
 * PostToolUse hook for Write / Edit / MultiEdit.
 *
 * Three jobs after every successful write:
 *
 *   1. `code-memory reingest <path>` — fire-and-forget. Tree-sitter
 *      re-parses, the file's symbol nodes + edges + Qdrant chunks are
 *      replaced.
 *   2. Invalidate the per-session Context Pack cache so the next user
 *      prompt re-fetches against the just-updated index.
 *   3. Schedule the cross-file resolver via a debounced detached worker
 *      (resolver-debounce.js). A burst of edits collapses to exactly one
 *      resolver run, 1.5s after the last write.
 *
 * Tool input arrives via stdin. Paths can live under different keys
 * (file_path, path, target) depending on the tool — we probe all of them.
 */

const path = require("node:path");
const { spawn } = require("node:child_process");
const { readEvent, done } = require("./lib/io");
const { createMemoryClient } = require("./lib/memory");
const { invalidatePack, touchResolverMarker } = require("./lib/state");

function pickPath(obj) {
  if (!obj || typeof obj !== "object") return null;
  for (const key of ["file_path", "filePath", "path", "target"]) {
    const v = obj[key];
    if (typeof v === "string" && v.length > 0) return v;
  }
  return null;
}

(async () => {
  const ev = await readEvent();
  const cwd = ev.cwd || process.cwd();
  const sessionId = ev.session_id || ev.sessionID || "unknown";
  const tool = String(ev.tool_name || ev.tool || "").toLowerCase();

  // Guard: matcher already restricts to Write|Edit|MultiEdit, but be defensive.
  if (!["write", "edit", "multiedit"].includes(tool)) {
    done();
    return;
  }

  const filePath =
    pickPath(ev.tool_input) ||
    pickPath(ev.tool_response) ||
    pickPath(ev.tool_response && ev.tool_response.args);

  if (!filePath) {
    done();
    return;
  }

  const mem = await createMemoryClient({ cwd, log: () => {} });
  if (!mem.available) {
    done();
    return;
  }

  // 1. reingest the file (background).
  mem.reingestDetached(filePath);

  // 2. drop cached pack for this session.
  invalidatePack(sessionId);

  // 3. schedule debounced resolver.
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
