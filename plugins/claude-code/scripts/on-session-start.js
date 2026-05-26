#!/usr/bin/env node
/**
 * SessionStart hook.
 *
 * Mirrors the OpenCode "first chat.message → background ingest" behavior.
 * Kicks off a one-shot `code-memory ingest <cwd>` (git-aware delta) so the
 * index reflects out-of-band edits (vim, IDE, git pull, git checkout) made
 * since the last session. Detached + fire-and-forget — never blocks startup.
 *
 * Source of `source`: "startup" | "resume" | "clear" | "compact". We treat
 * all of them the same; the CLI itself is cheap when nothing changed.
 */

const { readEvent, done } = require("./lib/io");
const { createMemoryClient } = require("./lib/memory");
const { pruneExpired } = require("./lib/state");

(async () => {
  const ev = await readEvent();
  const cwd = ev.cwd || process.cwd();
  const log = () => {}; // SessionStart must stay quiet; CLI logs to its own log file.

  pruneExpired();

  const mem = await createMemoryClient({ cwd, log });
  if (mem.available) {
    // Ensure a launchd/systemd watcher unit exists for this repo so file
    // edits between sessions trigger reingest automatically. Idempotent;
    // safety guard inside the CLI refuses home/root / non-VCS dirs.
    mem.autostartInstallDetached();
    mem.ingestDetached();
  }
  done();
})();
