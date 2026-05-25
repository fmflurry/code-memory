/**
 * Disk-backed session state. Hooks are fresh processes, so per-session
 * memory (first user message, last query, last fetched timestamp) must
 * persist between invocations.
 *
 * Layout: $CACHE_DIR/sessions/<session-id>.json
 *
 *   {
 *     "firstUserMessage": "...",
 *     "lastQuery": "...",
 *     "lastFetchedAt": 1737031234000,
 *     "bootstrapped": true
 *   }
 *
 * The PostToolUse hook drops the "lastQuery" field via `invalidatePack()`
 * so the next UserPromptSubmit refetches against the just-updated index.
 */

const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const crypto = require("node:crypto");

const SESSION_TTL_MS = 24 * 60 * 60 * 1000; // 24h — older session files get evicted lazily.

function cacheRoot() {
  const xdg = process.env.XDG_CACHE_HOME;
  const base = xdg ? xdg : path.join(os.homedir(), ".cache");
  return path.join(base, "code-memory", "claude-plugin");
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function sessionFile(sessionId) {
  const safe = String(sessionId || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 80);
  return path.join(cacheRoot(), "sessions", `${safe}.json`);
}

function cwdHash(cwd) {
  return crypto.createHash("sha1").update(String(cwd || "")).digest("hex").slice(0, 12);
}

function resolverMarkerFile(cwd) {
  return path.join(cacheRoot(), "resolvers", `${cwdHash(cwd)}.marker`);
}

function readJson(file) {
  try {
    const raw = fs.readFileSync(file, "utf8");
    return JSON.parse(raw);
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
    // swallow — hook must never crash the agent
  }
}

function loadSession(sessionId) {
  const data = readJson(sessionFile(sessionId));
  if (!data) {
    return {
      firstUserMessage: null,
      lastQuery: null,
      lastFetchedAt: 0,
      bootstrapped: false,
      retrieveSeen: false,
      turnGateNudged: false,
    };
  }
  return {
    firstUserMessage: data.firstUserMessage || null,
    lastQuery: data.lastQuery || null,
    lastFetchedAt: Number(data.lastFetchedAt || 0),
    bootstrapped: Boolean(data.bootstrapped),
    retrieveSeen: Boolean(data.retrieveSeen),
    turnGateNudged: Boolean(data.turnGateNudged),
  };
}

function markRetrieveSeen(sessionId) {
  const s = loadSession(sessionId);
  s.retrieveSeen = true;
  saveSession(sessionId, s);
}

function resetTurn(sessionId) {
  const s = loadSession(sessionId);
  s.retrieveSeen = false;
  s.turnGateNudged = false;
  saveSession(sessionId, s);
}

function markTurnGateNudged(sessionId) {
  const s = loadSession(sessionId);
  s.turnGateNudged = true;
  saveSession(sessionId, s);
}

function saveSession(sessionId, state) {
  writeJson(sessionFile(sessionId), state);
}

function invalidatePack(sessionId) {
  const s = loadSession(sessionId);
  s.lastQuery = null;
  s.lastFetchedAt = 0;
  saveSession(sessionId, s);
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
  invalidatePack,
  pruneExpired,
  touchResolverMarker,
  readResolverMarker,
  resolverMarkerFile,
  markRetrieveSeen,
  resetTurn,
  markTurnGateNudged,
};
