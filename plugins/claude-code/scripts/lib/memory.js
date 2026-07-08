/**
 * Thin shell wrapper over the `code-memory` Python CLI.
 *
 * Hooks are short-lived processes; every call is best-effort and bounded
 * by a per-call timeout. Errors are logged to the optional logger; they
 * never throw out of the module.
 */

const { execFile } = require("node:child_process");
const fs = require("node:fs");
const nodePath = require("node:path");

const DEFAULT_BINARY = process.env.CODE_MEMORY_BIN || "code-memory";
const DEFAULT_PROJECT = process.env.CODE_MEMORY_PROJECT || null;

function run(binary, args, opts = {}) {
  const { cwd, timeout = 8000, maxBuffer = 4 * 1024 * 1024, env } = opts;
  return new Promise((resolve) => {
    execFile(
      binary,
      args,
      { cwd, timeout, maxBuffer, env: env || process.env },
      (err, stdout, stderr) => {
        if (err) {
          resolve({ ok: false, err, stdout: String(stdout || ""), stderr: String(stderr || "") });
        } else {
          resolve({ ok: true, stdout: String(stdout || ""), stderr: String(stderr || "") });
        }
      },
    );
  });
}

function baseArgs(project) {
  return project ? ["--project", project] : [];
}

async function detectAvailable(binary, log) {
  const { ok, err } = await run(binary, ["--help"], { timeout: 3000 });
  if (!ok) log("debug", `binary ${binary} unavailable: ${err && err.message ? err.message : String(err)}`);
  return ok;
}

/**
 * Derive the lock directory used by code-memory's single_flight module.
 * Mirrors the Python logic in sync/single_flight.py:_lock_dir().
 * Returns null if the directory cannot be determined.
 */
function _lockDir() {
  const override = process.env.CODE_MEMORY_LOCK_DIR;
  if (override) return override;
  const stateHome =
    process.env.XDG_STATE_HOME ||
    nodePath.join(
      process.env.HOME || process.env.USERPROFILE || "",
      ".local",
      "state",
    );
  return nodePath.join(stateHome, "code-memory", "locks");
}

/**
 * Fast-path check: return true if a live ingest is already running for
 * (resolvedRoot, slug).  Mirrors Python single_flight._is_stale() logic:
 * a lockfile is considered live when it exists, is not older than the TTL,
 * and its PID is still running.
 *
 * This is JS-side best-effort only — the Python ingest entry point is the
 * authoritative single-flight guard.  We check here solely to avoid
 * spawning a new process that would immediately lose the race.
 *
 * @param {string} resolvedRoot - Absolute resolved path of the repo root.
 * @param {string} slug - Project slug.
 * @returns {boolean} true when a live ingest appears to be running.
 */
function _ingestLockLive(resolvedRoot, slug) {
  try {
    const lockDir = _lockDir();
    if (!lockDir) return false;

    // Replicate the Python filename derivation:
    //   name = f"{root_part[:64]}__{project_part[:32]}.lock"
    const rootPart = resolvedRoot.replace(/[/\\]/g, "_").replace(/ /g, "_").slice(0, 64);
    const slugPart = slug.replace(/\//g, "_").replace(/ /g, "_").slice(0, 32);
    const lockFile = nodePath.join(lockDir, `${rootPart}__${slugPart}.lock`);

    let stat;
    try {
      stat = fs.statSync(lockFile);
    } catch {
      return false; // file does not exist — no live ingest
    }

    const ttl = parseFloat(process.env.CODE_MEMORY_REBUILD_LOCK_TTL || "3600");
    const ageSecs = (Date.now() - stat.mtimeMs) / 1000;
    if (ageSecs > ttl) return false; // stale by age

    let pid;
    try {
      pid = parseInt(fs.readFileSync(lockFile, "utf8").trim(), 10);
    } catch {
      return false; // unreadable → treat as stale
    }
    if (!pid || isNaN(pid)) return false;

    // Check if the PID is alive (POSIX: signal 0; Windows: tasklist not used,
    // fall back to optimistic "assume live" to avoid a subprocess spawn).
    try {
      process.kill(pid, 0);
      return true; // PID exists and is alive
    } catch (e) {
      if (e.code === "EPERM") return true; // exists but we lack permission
      return false; // ESRCH — process is dead
    }
  } catch {
    return false; // any unexpected error → don't block
  }
}

/**
 * Resolve a windowless Python launcher on Windows.
 *
 * The `code-memory` command is a uv/pip **console-subsystem** shim (a uv
 * *trampoline* under uv installs). When spawned detached, the trampoline
 * re-launches `python.exe` — also console-subsystem — which allocates a
 * console window. Node's `windowsHide` only applies to the trampoline it
 * spawns, not to the interpreter the trampoline re-launches, so a cmd window
 * still flashes for every detached hook call.
 *
 * `pythonw.exe` is the GUI-subsystem interpreter: the OS never allocates a
 * console for it, regardless of `detached`/`windowsHide`. Under Node's
 * `stdio: "ignore"` it still receives valid NUL std handles, so `--json`
 * commands keep working (their output is discarded anyway). Returns the
 * pythonw path or null (→ fall back to the console shim).
 *
 * Resolution order: `CODE_MEMORY_PYTHONW` env override, then the uv tool
 * venv (`%APPDATA%/uv/tools/<*code-memory*>/Scripts/pythonw.exe`).
 */
function _windowlessPythonw() {
  if (process.platform !== "win32") return null;
  const override = process.env.CODE_MEMORY_PYTHONW;
  try {
    if (override && fs.existsSync(override)) return override;
  } catch {
    /* ignore */
  }
  const appdata = process.env.APPDATA;
  if (!appdata) return null;
  try {
    const toolsDir = nodePath.join(appdata, "uv", "tools");
    for (const name of fs.readdirSync(toolsDir)) {
      if (!/code[-_]memory/i.test(name)) continue;
      const pyw = nodePath.join(toolsDir, name, "Scripts", "pythonw.exe");
      if (fs.existsSync(pyw)) return pyw;
    }
  } catch {
    /* ignore — fall back to console shim */
  }
  return null;
}

/**
 * Spawn detached fire-and-forget. Parent exits immediately.
 * stdout/stderr ignored. Used when the hook must not block.
 *
 * On Windows, routes through pythonw.exe (see _windowlessPythonw) so no
 * console window is created; falls back to the `binary` shim elsewhere.
 */
function spawnDetached(binary, args, opts = {}) {
  const { spawn } = require("node:child_process");
  const { cwd, env } = opts;
  const pythonw = _windowlessPythonw();
  const cmd = pythonw || binary;
  const argv = pythonw ? ["-m", "code_memory.cli", ...args] : args;
  try {
    const child = spawn(cmd, argv, {
      cwd,
      env: env || process.env,
      detached: true,
      windowsHide: true,
      stdio: "ignore",
    });
    child.unref();
    return true;
  } catch {
    return false;
  }
}

async function createMemoryClient(opts = {}) {
  const log = opts.log || (() => {});
  const binary = opts.binary || DEFAULT_BINARY;
  const cwd = opts.cwd || process.cwd();
  const retrieveTimeout = opts.retrieveTimeoutMs || 8000;
  const mutateTimeout = opts.mutateTimeoutMs || 20000;
  const project = opts.project || DEFAULT_PROJECT;

  const available = await detectAvailable(binary, log);
  if (!available) log("warn", `code-memory binary not found on PATH (looked for: ${binary})`);

  return {
    available,
    project,
    cwd,

    async retrieve(query, { k, eps } = {}) {
      if (!available) return null;
      const args = [
        "retrieve",
        query,
        "--json",
        ...(k ? ["--k", String(k)] : []),
        ...(eps ? ["--eps", String(eps)] : []),
        ...baseArgs(project),
      ];
      const r = await run(binary, args, { cwd, timeout: retrieveTimeout });
      if (!r.ok) {
        log("warn", `retrieve failed: ${r.err && r.err.message ? r.err.message : "?"}`);
        return null;
      }
      try {
        return JSON.parse(r.stdout);
      } catch (e) {
        log("warn", `retrieve JSON parse failed: ${e.message}`);
        return null;
      }
    },

    reingestDetached(path) {
      if (!available) return false;
      return spawnDetached(
        binary,
        ["reingest", path, "--json", ...baseArgs(project)],
        { cwd },
      );
    },

    resolveDetached() {
      if (!available) return false;
      return spawnDetached(binary, ["resolve", "--json", ...baseArgs(project)], { cwd });
    },

    ingestDetached({ full = false } = {}) {
      if (!available) return false;
      // Fast-path: skip spawn if the Python single-flight lock shows a live
      // ingest is already running for this root.  The CLI is the authoritative
      // guard; this avoids spawning a process that would immediately lose the
      // race and exit with code 0.
      const resolvedCwd = nodePath.resolve(cwd);
      const slug = project || nodePath.basename(resolvedCwd);
      if (_ingestLockLive(resolvedCwd, slug)) return false;
      return spawnDetached(
        binary,
        ["ingest", cwd, "--json", ...(full ? ["--full"] : []), ...baseArgs(project)],
        { cwd },
      );
    },

    autostartInstallDetached() {
      if (!available) return false;
      // Idempotent: `ensure_autostart` no-ops if launchd unit already
      // installed and running. Passes safety guard (refuses home/root
      // / non-VCS dirs) so unsafe cwds are silently skipped.
      return spawnDetached(
        binary,
        ["autostart", "install", cwd, "--json"],
        { cwd },
      );
    },

    async record({ prompt, plan, patch, verdict }) {
      if (!available) return;
      const args = [
        "record",
        "--prompt",
        prompt,
        ...(plan ? ["--plan", plan] : []),
        ...(patch ? ["--patch", patch] : []),
        ...(verdict ? ["--verdict", verdict] : []),
        "--json",
        ...baseArgs(project),
      ];
      const r = await run(binary, args, { cwd, timeout: mutateTimeout });
      if (!r.ok) log("warn", `record failed: ${r.err && r.err.message ? r.err.message : "?"}`);
    },
  };
}

module.exports = { createMemoryClient, _windowlessPythonw };
