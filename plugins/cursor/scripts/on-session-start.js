#!/usr/bin/env node
/**
 * sessionStart hook.
 *
 * Fires once when a new Cursor Composer conversation starts. Best spot
 * to refresh the index for out-of-band edits (vim, IDE, `git pull`,
 * `git checkout`) since the previous session.
 *
 * Background-detached so we never block Cursor's first message.
 */

const { readEvent, done } = require("./lib/io");
const { createMemoryClient } = require("./lib/memory");
const { pruneExpired } = require("./lib/state");

(async () => {
  const ev = await readEvent();
  const cwd = pickCwd(ev) || process.cwd();

  pruneExpired();

  const mem = await createMemoryClient({ cwd, log: () => {} });
  if (mem.available) {
    mem.ingestDetached();
    mem.autostartInstallDetached();
  }

  done();
})();

function pickCwd(ev) {
  if (Array.isArray(ev.workspace_roots) && ev.workspace_roots.length > 0) {
    return ev.workspace_roots[0];
  }
  return process.env.CURSOR_PROJECT_DIR || null;
}
