#!/usr/bin/env node
/**
 * beforeMCPExecution hook (matched on codememory_* tools via hooks.json).
 *
 * Marks the per-turn gate flag as satisfied so subsequent shell / read /
 * grep calls in the same turn skip the nudge.
 */

const { readEvent, done } = require("./lib/io");
const { markGateSatisfied } = require("./lib/state");

(async () => {
  const ev = await readEvent();
  const convId = ev.conversation_id || "unknown";
  const tool = String(ev.tool_name || "");

  if (tool.toLowerCase().includes("codememory_")) {
    markGateSatisfied(convId);
  }

  done({ permission: "allow" });
})();
