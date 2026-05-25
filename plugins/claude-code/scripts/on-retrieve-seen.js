#!/usr/bin/env node
/**
 * PostToolUse hook that fires when the agent calls any code-memory MCP
 * tool that counts as "the agent used the memory layer this turn". Once
 * one of these fires, the PreToolUse gate stays silent for the rest of
 * the turn.
 *
 * Matcher is regex over MCP tool names: see hooks.json.
 */

const { readEvent, done } = require("./lib/io");
const { markRetrieveSeen } = require("./lib/state");

(async () => {
  const ev = await readEvent();
  const sessionId = ev.session_id || ev.sessionID || "unknown";
  // Explicit agent MCP/tool use satisfies the filesystem/search/shell gate.
  markRetrieveSeen(sessionId);
  done();
})();
