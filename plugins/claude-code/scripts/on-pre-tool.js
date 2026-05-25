#!/usr/bin/env node
/**
 * PreToolUse gate.
 *
 * Fires before Read / Bash / Grep / Glob. If no explicit code-memory MCP
 * tool has happened yet this turn, we request a soft reminder via
 * additionalContext. Depending on host timing, it may affect the current or
 * next model context. We never block — the queued tool still runs.
 *
 * Single-shot per turn: once nudged, subsequent reads / greps in the same
 * turn stay quiet. Resets on the next UserPromptSubmit (see resetTurn()).
 *
 * Auto-retrieve on UserPromptSubmit only injects orientation. It does not
 * satisfy this explicit-use gate.
 */

const { readEvent, done } = require("./lib/io");
const { loadSession, markTurnGateNudged } = require("./lib/state");

const GATED_TOOLS = new Set(["read", "bash", "grep", "glob"]);

const NUDGE = [
  "## code-memory gate",
  "",
  "Soft nudge: a filesystem / shell tool was queued without first making an",
  "explicit code-memory MCP call this turn. Depending on host timing, this",
  "may affect current or next model context; the queued tool still runs.",
  "The auto-injected Context Pack",
  "is orientation only; it is not enough for this gate. For codebase questions",
  "(where is X, how does Y work, who calls Z, where are the docs) call",
  "`codememory_retrieve` first, then use filesystem tools only to verify:",
  "",
  "- `mcp__code-memory__codememory_retrieve` — semantic + episodic recall",
  "- `mcp__code-memory__codememory_definitions` — exact symbol locations",
  "- `mcp__code-memory__codememory_callers` / `_callees` — call graph",
  "- `mcp__code-memory__codememory_importers` / `_dependencies` — imports",
  "",
  "Docs inventory / repo documentation / where docs live: call retrieve first,",
  "then glob/read for exhaustive verification.",
  "",
  "Prefer one targeted MCP call before scanning the filesystem. This is a",
  "one-time per-turn nudge.",
  "",
  "_Skip the nudge by explicitly querying code-memory first, or by ignoring it if the",
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
