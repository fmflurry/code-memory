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
const { loadSession, saveSession } = require("./lib/state");
const { formatPack } = require("./lib/format");

const DEDUP_WINDOW_MS = 60 * 1000;

(async () => {
  const ev = await readEvent();
  const cwd = ev.cwd || process.cwd();
  const sessionId = ev.session_id || ev.sessionID || "unknown";
  const prompt = String(ev.prompt || "");

  const state = loadSession(sessionId);
  if (!state.firstUserMessage && prompt.trim()) {
    state.firstUserMessage = prompt.trim();
    saveSession(sessionId, state);
  }

  if (!isSubstantiveCodeIntent(prompt)) {
    done();
    return;
  }

  const query = extractQueryFromMessage(prompt);
  const now = Date.now();
  if (
    state.lastQuery === query &&
    state.lastFetchedAt &&
    now - state.lastFetchedAt < DEDUP_WINDOW_MS
  ) {
    done();
    return;
  }

  const mem = await createMemoryClient({ cwd, log: () => {} });
  if (!mem.available) {
    done();
    return;
  }

  const pack = await mem.retrieve(query, { k: 8, eps: 5 });
  if (!pack) {
    done();
    return;
  }

  const isEmpty =
    (!Array.isArray(pack.code) || pack.code.length === 0) &&
    (!Array.isArray(pack.episodes) || pack.episodes.length === 0);

  state.lastQuery = query;
  state.lastFetchedAt = Date.now();
  saveSession(sessionId, state);

  if (isEmpty) {
    done();
    return;
  }

  const additionalContext = formatPack(pack);
  done({
    hookSpecificOutput: {
      hookEventName: "UserPromptSubmit",
      additionalContext,
    },
  });
})();
