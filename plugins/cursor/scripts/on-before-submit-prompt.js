#!/usr/bin/env node
/**
 * beforeSubmitPrompt hook.
 *
 * Behavior:
 *   1. Reset the per-turn gate flags so the next tool execution can
 *      re-evaluate whether the agent hit code-memory first.
 *   2. Capture the first user message (used as the episode prompt
 *      on `stop`).
 *   3. Detect durable claim intent and stash a one-shot nudge to disk.
 *      Cursor's `beforeSubmitPrompt` output does not support
 *      `additional_context`, so the nudge is drained by the first
 *      `postToolUse` of the turn (one of the two hooks Cursor lets
 *      inject context).
 *
 * Always returns `{continue: true}` — never blocks user submission.
 */

const { readEvent, done } = require("./lib/io");
const { loadSession, saveSession, resetTurn } = require("./lib/state");
const { detectClaimIntent, formatClaimNudge } = require("./lib/claim-intent");

(async () => {
  const ev = await readEvent();
  const convId = ev.conversation_id || "unknown";
  const prompt = String(ev.prompt || "");

  resetTurn(convId);

  const state = loadSession(convId);
  if (!state.firstUserMessage && prompt.trim()) {
    state.firstUserMessage = prompt.trim();
  }

  const hit = detectClaimIntent(prompt);
  if (hit) {
    state.pendingClaimNudge = formatClaimNudge(hit);
  }

  saveSession(convId, state);

  done({ continue: true });
})();
