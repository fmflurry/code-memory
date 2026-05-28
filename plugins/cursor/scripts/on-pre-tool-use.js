#!/usr/bin/env node
/**
 * preToolUse hook for Shell / Read / Grep tools.
 *
 * First-tool gate: if no codememory_* MCP tool has fired this turn
 * AND we haven't already nudged this turn, surface a soft reminder
 * via the `agent_message` field. Never blocks — the queued tool
 * always runs.
 *
 * The matcher in hooks.json restricts which tools we see, but we
 * defensively re-check on `tool_name`.
 */

const { readEvent, done } = require("./lib/io");
const { loadSession, markGateNudged } = require("./lib/state");

const GATED = new Set(["Shell", "Read", "Grep"]);

const NUDGE = [
  "## code-memory gate",
  "",
  "A filesystem / shell tool is about to run without first making an",
  "explicit code-memory MCP call this turn. The queued tool still runs.",
  "For codebase questions (where is X, how does Y work, who calls Z,",
  "where are the docs) call `codememory_retrieve` first, then use",
  "filesystem tools only to verify:",
  "",
  "- `codememory_retrieve` — semantic + episodic recall",
  "- `codememory_definitions` — exact symbol locations",
  "- `codememory_callers` / `codememory_callees` — call graph",
  "- `codememory_importers` / `codememory_dependencies` — imports",
  "",
  "Prefer one targeted MCP call before scanning the filesystem.",
].join("\n");

(async () => {
  const ev = await readEvent();
  const convId = ev.conversation_id || "unknown";
  const tool = String(ev.tool_name || "");

  if (!GATED.has(tool)) {
    done({ permission: "allow" });
    return;
  }

  const state = loadSession(convId);
  if (state.gateSatisfied || state.gateNudged) {
    done({ permission: "allow" });
    return;
  }

  markGateNudged(convId);
  done({ permission: "allow", agent_message: NUDGE });
})();
