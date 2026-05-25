/**
 * Thin shell wrapper over the `code-memory` Python CLI.
 *
 * Hooks are short-lived processes; every call is best-effort and bounded
 * by a per-call timeout. Errors are logged to the optional logger; they
 * never throw out of the module.
 */

const { execFile } = require("node:child_process");

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
 * Spawn detached fire-and-forget. Parent exits immediately.
 * stdout/stderr ignored. Used when the hook must not block.
 */
function spawnDetached(binary, args, opts = {}) {
  const { spawn } = require("node:child_process");
  const { cwd, env } = opts;
  try {
    const child = spawn(binary, args, {
      cwd,
      env: env || process.env,
      detached: true,
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
      return spawnDetached(
        binary,
        ["ingest", cwd, "--json", ...(full ? ["--full"] : []), ...baseArgs(project)],
        { cwd },
      );
    },

    /**
     * Fire-and-forget claim extraction over one or more user prompts.
     * No-op when `CLAIMS_EXTRACTION=true` is not set on the daemon side —
     * the CLI exits 0 with a "disabled" payload, so the spawn is cheap.
     */
    extractClaimsDetached({ prompts, sessionId } = {}) {
      if (!available) return false;
      const list = (prompts || []).filter((p) => typeof p === "string" && p.trim());
      if (!list.length) return false;
      const args = ["extract-claims", "--json", ...baseArgs(project)];
      for (const p of list) {
        args.push("--prompt", p);
      }
      if (sessionId) args.push("--session-id", String(sessionId));
      return spawnDetached(binary, args, { cwd });
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

module.exports = { createMemoryClient };
