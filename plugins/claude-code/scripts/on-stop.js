#!/usr/bin/env node
/**
 * Stop hook.
 *
 * Equivalent of OpenCode's `session.idle` listener: when the agent
 * finishes a response (or the user stops it), record the session as an
 * episode so future sessions can recall what was attempted.
 *
 * Inputs we need:
 *   - The first user message of this session (loaded from disk state).
 *   - A `git diff` in the cwd as the patch payload.
 *
 * `verdict` is set to "idle" — same convention as the OpenCode plugin —
 * to mark that this is a passive recording rather than an explicit
 * `codememory_record(...)` call from the agent.
 *
 * If `stop_hook_active` is true on the event (Claude Code re-invoked
 * itself after a Stop block), we skip to avoid recording the same
 * session repeatedly within a single turn.
 */

const { execFile } = require("node:child_process");
const { readEvent, done } = require("./lib/io");
const { createMemoryClient } = require("./lib/memory");
const { loadSession } = require("./lib/state");

function gitDiff(cwd) {
  return new Promise((resolve) => {
    execFile(
      "git",
      ["-C", cwd, "diff", "--unified=0"],
      { timeout: 4000, maxBuffer: 1024 * 1024 },
      (err, stdout) => resolve(err ? "" : String(stdout || "").trim()),
    );
  });
}

(async () => {
  const ev = await readEvent();
  if (ev.stop_hook_active) {
    done();
    return;
  }

  const cwd = ev.cwd || process.cwd();
  const sessionId = ev.session_id || ev.sessionID || "unknown";
  const state = loadSession(sessionId);
  if (!state.firstUserMessage) {
    done();
    return;
  }

  const mem = await createMemoryClient({ cwd, log: () => {} });
  if (!mem.available) {
    done();
    return;
  }

  const patch = await gitDiff(cwd);
  await mem.record({
    prompt: state.firstUserMessage,
    patch: patch || undefined,
    verdict: "idle",
  });

  // Claims are authored explicitly by the agent via
  // `codememory_assert_claim` when it judges a message claim-worthy.
  // The LLM extraction path was removed.
  done();
})();
