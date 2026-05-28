#!/usr/bin/env node
/**
 * sessionEnd hook.
 *
 * Fire-and-forget cleanup of the per-conversation state file. The
 * resolver-marker (per cwd, not per conversation) is left in place;
 * the next session will reuse it.
 */

const { readEvent, done } = require("./lib/io");
const { clearSession } = require("./lib/state");

(async () => {
  const ev = await readEvent();
  const convId = ev.conversation_id || ev.session_id || null;
  if (convId) clearSession(convId);
  done();
})();
