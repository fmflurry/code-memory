#!/usr/bin/env node
/**
 * preCompact hook.
 *
 * Cursor compaction collapses context to fit the window. Record an
 * eager episode here so the work-in-progress isn't lost to history.
 * No equivalent exists in the Claude Code or OpenCode plugins —
 * Cursor-only feature worth using.
 */

const { readEvent, done } = require("./lib/io");
const { createMemoryClient } = require("./lib/memory");
const { loadSession } = require("./lib/state");
const { execFileSync } = require("node:child_process");

(async () => {
  const ev = await readEvent();
  const convId = ev.conversation_id || "unknown";
  const cwd =
    (Array.isArray(ev.workspace_roots) && ev.workspace_roots[0]) ||
    process.env.CURSOR_PROJECT_DIR ||
    process.cwd();

  const state = loadSession(convId);
  if (!state.firstUserMessage) {
    done();
    return;
  }

  const mem = await createMemoryClient({ cwd, log: () => {} });
  if (!mem.available) {
    done();
    return;
  }

  let patch = "";
  try {
    patch = execFileSync("git", ["-C", cwd, "diff", "--unified=0"], {
      timeout: 4000,
      maxBuffer: 1024 * 1024,
      encoding: "utf8",
    }).trim();
  } catch {
    // ignore — record still useful without patch
  }

  await mem.record({
    prompt: state.firstUserMessage,
    patch,
    verdict: "idle",
  });

  done();
})();
