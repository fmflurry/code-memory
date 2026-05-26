#!/usr/bin/env node
/**
 * UserPromptSubmit hook.
 *
 * Replaces both OpenCode hooks `chat.message` (fetch) and
 * `experimental.chat.system.transform` (inject) in one shot, because
 * Claude Code lets a single hook return `additionalContext` that is
 * appended to the user message before the model sees it.
 *
 * Behavior:
 *   1. Detect substantive code intent (intent.js). Trivial follow-ups
 *      ("yes", "continue", "thanks") short-circuit.
 *   2. Dedup against the last query for this session within 60s — same
 *      query within the dedup window returns no extra context.
 *   3. Call `code-memory retrieve --json` (8s timeout).
 *   4. Format the pack via format.js and emit it as `additionalContext`
 *      in the hookSpecificOutput envelope.
 *   5. Persist session state (firstUserMessage, lastQuery, lastFetchedAt)
 *      to disk so PostToolUse / Stop / next UserPromptSubmit can reuse it.
 *
 * On every error the hook exits cleanly with no additional context — the
 * agent's turn is never blocked, even if the backend is down.
 */

const { readEvent, done } = require("./lib/io");
const { createMemoryClient } = require("./lib/memory");
const { isSubstantiveCodeIntent, extractQueryFromMessage } = require("./lib/intent");
const { detectClaimIntent, formatClaimNudge } = require("./lib/claim-intent");
const { loadSession, saveSession, resetTurn, markAutoRetrieveSeen } = require("./lib/state");
const { formatPack } = require("./lib/format");

const DEDUP_WINDOW_MS = 60 * 1000;

(async () => {
  const ev = await readEvent();
  const cwd = ev.cwd || process.cwd();
  const sessionId = ev.session_id || ev.sessionID || "unknown";
  const prompt = String(ev.prompt || "");

  // New turn: clear gate flags before any retrieve/exploration begins.
  resetTurn(sessionId);

  const state = loadSession(sessionId);
  if (!state.firstUserMessage && prompt.trim()) {
    state.firstUserMessage = prompt.trim();
    saveSession(sessionId, state);
  }

  // Claim-intent nudge runs independently of code-retrieval: a message
  // like "I love Clean Architecture" carries no code-search signal but
  // is exactly the kind of durable assertion the agent must capture.
  const claimHit = detectClaimIntent(prompt);
  const claimNudge = claimHit ? formatClaimNudge(claimHit) : null;

  const finish = (packContext) => {
    const parts = [];
    if (claimNudge) parts.push(claimNudge);
    if (packContext) parts.push(packContext);
    if (parts.length === 0) {
      done();
      return;
    }
    done({
      hookSpecificOutput: {
        hookEventName: "UserPromptSubmit",
        additionalContext: parts.join("\n\n"),
      },
    });
  };

  if (!isSubstantiveCodeIntent(prompt)) {
    finish(null);
    return;
  }

  const query = extractQueryFromMessage(prompt);
  const now = Date.now();
  if (
    state.lastQuery === query &&
    state.lastFetchedAt &&
    now - state.lastFetchedAt < DEDUP_WINDOW_MS
  ) {
    finish(null);
    return;
  }

  const mem = await createMemoryClient({ cwd, log: () => {} });
  if (!mem.available) {
    finish(null);
    return;
  }

  const pack = await mem.retrieve(query, { k: 8, eps: 5 });
  if (!pack) {
    finish(null);
    return;
  }

  const isEmpty =
    (!Array.isArray(pack.code) || pack.code.length === 0) &&
    (!Array.isArray(pack.episodes) || pack.episodes.length === 0);

  state.lastQuery = query;
  state.lastFetchedAt = Date.now();
  saveSession(sessionId, state);
  // Auto-retrieve fired, but only an explicit MCP tool call satisfies the gate.
  markAutoRetrieveSeen(sessionId);

  finish(isEmpty ? null : formatPack(pack));
})();
