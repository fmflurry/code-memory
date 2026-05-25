#!/usr/bin/env node
/**
 * PreToolUse gate.
 *
 * Fires before Read / Bash / Grep / Glob. If no `codememory_retrieve` has
 * happened yet this turn (auto-retrieve on UserPromptSubmit OR an explicit
 * MCP call) we inject a soft reminder into the tool's context so the agent
 * sees it BEFORE running the command. We never block — the tool still runs.
 *
 * Single-shot per turn: once nudged, subsequent reads / greps in the same
 * turn stay quiet. Resets on the next UserPromptSubmit (see resetTurn()).
 *
 * No nudge is emitted for codememory_* MCP tools themselves, and the gate
 * silences itself if the retrieve flag is already set.
 */

const { readEvent, done } = require("./lib/io");
const { loadSession, markTurnGateNudged } = require("./lib/state");

const GATED_TOOLS = new Set(["read", "bash", "grep", "glob"]);

const NUDGE = [
  "## code-memory gate",
  "",
  "You're about to run a filesystem / shell tool without first querying",
  "code-memory this turn. For codebase questions (where is X, how does Y",
  "work, who calls Z, where are the docs) the local index gives a precise",
  "answer in one call:",
  "",
  "- `mcp__code-memory__codememory_retrieve` — semantic + episodic recall",
  "- `mcp__code-memory__codememory_definitions` — exact symbol locations",
  "- `mcp__code-memory__codememory_callers` / `_callees` — call graph",
  "- `mcp__code-memory__codememory_importers` / `_dependencies` — imports",
  "",
  "Consider one targeted MCP call before scanning the filesystem. The tool",
  "you queued will still run; this is a one-time per-turn nudge.",
  "",
  "_Skip the nudge by querying code-memory first, or by ignoring it if the",
  "task genuinely needs raw shell (running tests, checking processes, etc.)._",
].join("\n");

(async () => {
  const ev = await readEvent();
  const sessionId = ev.session_id || ev.sessionID || "unknown";
  const tool = String(ev.tool_name || ev.tool || "").toLowerCase();

  if (!GATED_TOOLS.has(tool)) {
    done();
    return;
  }

  const state = loadSession(sessionId);
  if (state.retrieveSeen || state.turnGateNudged) {
    done();
    return;
  }

  markTurnGateNudged(sessionId);

  done({
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      additionalContext: NUDGE,
    },
  });
})();
