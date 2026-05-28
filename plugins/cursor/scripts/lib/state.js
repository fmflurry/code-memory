/**
 * Disk-backed per-conversation state for the Cursor plugin.
 *
 * Cursor hooks are fresh processes (same model as Claude Code), so any
 * state shared across hook invocations within a conversation must live
 * on disk.
 *
 * Layout: $CACHE_DIR/sessions/<conv-id>.json
 *
 *   {
 *     "firstUserMessage": "...",
 *     "gateSatisfied": false,      // a codememory_* MCP tool fired this turn
 *     "gateNudged": false,         // gate nudge already surfaced this turn
 *     "pendingClaimNudge": "..."   // queued for next postToolUse drain
 *   }
 *
 * Cursor's payload uses `conversation_id` (common field) — we key on that.
 */

const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const crypto = require("node:crypto");

const SESSION_TTL_MS = 24 * 60 * 60 * 1000;

function cacheRoot() {
  const xdg = process.env.XDG_CACHE_HOME;
  const base = xdg ? xdg : path.join(os.homedir(), ".cache");
  return path.join(base, "code-memory", "cursor-plugin");
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function sessionFile(convId) {
  const safe = String(convId || "unknown")
    .replace(/[^a-zA-Z0-9_-]/g, "_")
    .slice(0, 80);
  return path.join(cacheRoot(), "sessions", `${safe}.json`);
}

function cwdHash(cwd) {
  return crypto
    .createHash("sha1")
    .update(String(cwd || ""))
    .digest("hex")
    .slice(0, 12);
}

function resolverMarkerFile(cwd) {
  return path.join(cacheRoot(), "resolvers", `${cwdHash(cwd)}.marker`);
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

function writeJson(file, data) {
  try {
    ensureDir(path.dirname(file));
    const tmp = `${file}.tmp.${process.pid}`;
    fs.writeFileSync(tmp, JSON.stringify(data));
    fs.renameSync(tmp, file);
  } catch {
    // hook must never crash the agent
  }
}

function loadSession(convId) {
  const data = readJson(sessionFile(convId));
  if (!data) {
    return {
      firstUserMessage: null,
      gateSatisfied: false,
      gateNudged: false,
      pendingClaimNudge: null,
    };
  }
  return {
    firstUserMessage: data.firstUserMessage || null,
    gateSatisfied: Boolean(data.gateSatisfied),
    gateNudged: Boolean(data.gateNudged),
    pendingClaimNudge: data.pendingClaimNudge || null,
  };
}

function saveSession(convId, state) {
  writeJson(sessionFile(convId), state);
}

function resetTurn(convId) {
  const s = loadSession(convId);
  s.gateSatisfied = false;
  s.gateNudged = false;
  saveSession(convId, s);
}

function markGateSatisfied(convId) {
  const s = loadSession(convId);
  s.gateSatisfied = true;
  saveSession(convId, s);
}

function markGateNudged(convId) {
  const s = loadSession(convId);
  s.gateNudged = true;
  saveSession(convId, s);
}

function clearSession(convId) {
  try {
    fs.unlinkSync(sessionFile(convId));
  } catch {
    // ignore
  }
}

function pruneExpired() {
  const dir = path.join(cacheRoot(), "sessions");
  let entries;
  try {
    entries = fs.readdirSync(dir);
  } catch {
    return;
  }
  const now = Date.now();
  for (const entry of entries) {
    const full = path.join(dir, entry);
    try {
      const st = fs.statSync(full);
      if (now - st.mtimeMs > SESSION_TTL_MS) fs.unlinkSync(full);
    } catch {
      // ignore
    }
  }
}

function touchResolverMarker(cwd) {
  const file = resolverMarkerFile(cwd);
  ensureDir(path.dirname(file));
  const now = Date.now();
  try {
    fs.writeFileSync(file, String(now));
  } catch {
    // ignore
  }
  return now;
}

function readResolverMarker(cwd) {
  try {
    return Number(fs.readFileSync(resolverMarkerFile(cwd), "utf8")) || 0;
  } catch {
    return 0;
  }
}

module.exports = {
  cacheRoot,
  loadSession,
  saveSession,
  resetTurn,
  markGateSatisfied,
  markGateNudged,
  clearSession,
  pruneExpired,
  touchResolverMarker,
  readResolverMarker,
  resolverMarkerFile,
};
