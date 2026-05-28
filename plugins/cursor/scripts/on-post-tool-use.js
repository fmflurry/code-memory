#!/usr/bin/env node
/**
 * postToolUse hook — only used to drain pending claim-intent nudges.
 *
 * Cursor's `beforeSubmitPrompt` cannot inject context. `postToolUse`
 * can, via `additional_context`. So when `beforeSubmitPrompt` detects
 * a durable user assertion ("we use X", "I prefer Y", "don't ship Z"),
 * it stashes the nudge to disk; the first `postToolUse` of the turn
 * drains it.
 *
 * One-shot: the field is cleared after draining so the nudge does not
 * resurface on every subsequent tool call.
 */

const { readEvent, done } = require("./lib/io");
const { loadSession, saveSession } = require("./lib/state");

(async () => {
  const ev = await readEvent();
  const convId = ev.conversation_id || "unknown";

  const state = loadSession(convId);
  if (!state.pendingClaimNudge) {
    done();
    return;
  }

  const nudge = state.pendingClaimNudge;
  state.pendingClaimNudge = null;
  saveSession(convId, state);

  done({ additional_context: nudge });
})();
