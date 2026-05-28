#!/usr/bin/env node
/**
 * UserPromptSubmit hook.
 *
 * Behavior:
 *   1. Reset per-turn gate flags so the next PreToolUse pass can re-evaluate.
 *   2. Capture the first user message in session state (used as the episode
 *      prompt on Stop).
 *   3. Detect durable claim intent ("I prefer X", "we use Y", "don't ship Z")
 *      and emit a one-shot nudge reminding the agent to call
 *      `codememory_assert_claim`. Surfaces as `additionalContext` so the
 *      model sees it before planning the turn.
 *
 * No automatic Context Pack injection — the agent calls
 * `codememory_retrieve` itself when the gate nudge / rules push it there.
 * Every error path exits cleanly; the agent's turn is never blocked.
 */

const { readEvent, done } = require("./lib/io");
const { detectClaimIntent, formatClaimNudge } = require("./lib/claim-intent");
const { loadSession, saveSession, resetTurn } = require("./lib/state");

(async () => {
  const ev = await readEvent();
  const sessionId = ev.session_id || ev.sessionID || "unknown";
  const prompt = String(ev.prompt || "");

  resetTurn(sessionId);

  const state = loadSession(sessionId);
  if (!state.firstUserMessage && prompt.trim()) {
    state.firstUserMessage = prompt.trim();
    saveSession(sessionId, state);
  }

  const claimHit = detectClaimIntent(prompt);
  if (!claimHit) {
    done();
    return;
  }

  done({
    hookSpecificOutput: {
      hookEventName: "UserPromptSubmit",
      additionalContext: formatClaimNudge(claimHit),
    },
  });
})();
