/**
 * code-memory OpenCode plugin
 *
 * - Surface a claim-intent nudge when the user message contains a durable
 *   assertion (preference / rejection / ownership / location).
 * - Steer the agent toward `codememory_retrieve` before grep / read / shell
 *   via a one-shot gate nudge and an injected tool-description prefix.
 * - Auto-learn: hook `tool.execute.after` to call `code-memory reingest <path>`
 *   whenever the agent writes or edits a file. Hook `event session.idle` to
 *   record the session as an episode (best-effort).
 *
 * All backend calls are best-effort: a missing or broken `code-memory` CLI
 * degrades to no-op without breaking the session.
 */

import { execFile as execFileCb } from "node:child_process";
import { existsSync } from "node:fs";
import * as nodeOs from "node:os";
import * as nodePath from "node:path";
import { promisify } from "node:util";

import type { Plugin } from "@opencode-ai/plugin";

import {
  type LogLevel,
  type MemoryClient,
  createMemoryClient,
} from "./code-memory-lib/memory-client.ts";
import {
  detectClaimIntent,
  formatClaimNudge,
} from "./code-memory-lib/claim-intent.ts";

const execFile = promisify(execFileCb);

// After the *last* write in a burst, wait this long before re-running the
// resolver. Keeps high-frequency edit storms from spawning N resolver runs.
const RESOLVER_DEBOUNCE_MS = 1500;
const WRITE_TOOLS: ReadonlySet<string> = new Set(["write", "edit", "patch"]);
const GATED_READ_TOOLS: ReadonlySet<string> = new Set([
  "read",
  "bash",
  "grep",
  "glob",
]);
const MEMORY_TOOL_PREFIXES: readonly string[] = [
  "codememory_",
  "code-memory_",
  "code_memory_",
  "mcp__code-memory__",
];
const SERVICE = "code-memory";

/**
 * Returns true when `root` is safe to ingest / autostart-register.
 * Mirrors the Python `assert_safe_ingest_root` logic in sync/safety.py.
 *
 * A root is UNSAFE when it is:
 *   - empty / undefined
 *   - the filesystem root (`/` on POSIX, `C:\` etc. on Windows)
 *   - the user's $HOME directory itself
 *   - not inside a VCS repository (no `.git` in cwd or any ancestor)
 *
 * We perform the fast structural checks here (no subprocess). The VCS
 * check is a best-effort synchronous walk up the directory tree; if it
 * cannot determine VCS membership it conservatively returns `false`.
 */
function isSafeProjectRoot(root: string): boolean {
  if (!root || root.trim().length === 0) return false;

  const resolved = nodePath.resolve(root);

  // Filesystem root: resolve() of "/" → "/" on POSIX; drive roots on Windows.
  const parsed = nodePath.parse(resolved);
  if (parsed.root === resolved + nodePath.sep || parsed.root === resolved) {
    return false;
  }

  // Home directory.
  try {
    const home = nodeOs.homedir();
    if (home && nodePath.resolve(home) === resolved) return false;
  } catch {
    // homedir() failed — defensive skip, don't block
  }

  // VCS check: walk up from `resolved` looking for a `.git` entry.
  // Stops at the filesystem root to avoid an infinite loop.
  let dir = resolved;
  for (;;) {
    // Synchronous existsSync is intentional: this guard is called at
    // session-bootstrap time and must return a synchronous boolean.
    if (existsSync(nodePath.join(dir, ".git"))) return true;
    const parent = nodePath.dirname(dir);
    if (parent === dir) {
      // Reached filesystem root without finding .git.
      return false;
    }
    dir = parent;
  }
}

const GATE_NUDGE = [
  "## code-memory gate",
  "",
  "Your previous tool calls hit the filesystem / shell without first making",
  "an explicit code-memory MCP call. For codebase questions (where is X, how",
  "does Y work, who calls Z, where are the docs) call `codememory_retrieve`",
  "first, then use filesystem tools only to verify:",
  "",
  "- `codememory_retrieve` — semantic + episodic recall",
  "- `codememory_definitions` — exact symbol locations",
  "- `codememory_callers` / `codememory_callees` — call graph",
  "- `codememory_importers` / `codememory_dependencies` — imports",
  "- `codememory_health` — backend status + collection stats",
  "",
  "Default to one targeted MCP call before scanning the filesystem.",
].join("\n");

interface SessionMemory {
  firstUserMessage: string | null;
  explicitMemorySeen: boolean;
  pendingGateNudge: boolean;
  pendingClaimNudge: string | null;
}

function isMemoryTool(tool: string): boolean {
  const lower = tool.toLowerCase();
  return MEMORY_TOOL_PREFIXES.some((p) => lower.includes(p));
}

interface ToolInput {
  readonly tool?: string;
  readonly sessionID?: string;
  readonly callID?: string;
}

interface ToolOutput {
  readonly args?: Record<string, unknown>;
  readonly metadata?: Record<string, unknown>;
}

interface ChatMessageInput {
  readonly sessionID?: string;
}

interface ChatMessageOutput {
  readonly parts?: ReadonlyArray<{ type?: string; text?: string }>;
}

interface SystemTransformOutput {
  system?: string[];
}

interface ToolDefinitionInput {
  readonly toolID: string;
}

interface ToolDefinitionOutput {
  description: string;
  parameters: unknown;
}

interface EventEnvelope {
  readonly type?: string;
  readonly properties?: Record<string, unknown>;
}

function pickToolPath(args: Record<string, unknown> | undefined): string | null {
  if (!args) return null;
  for (const key of ["filePath", "file_path", "path", "target"]) {
    const v = args[key];
    if (typeof v === "string" && v.length > 0) return v;
  }
  return null;
}

function extractText(parts: ChatMessageOutput["parts"] | undefined): string {
  if (!Array.isArray(parts)) return "";
  return parts
    .filter((p): p is { type?: string; text: string } => typeof p?.text === "string")
    .map((p) => p.text)
    .join("\n")
    .trim();
}

async function gitDiff(cwd: string): Promise<string> {
  try {
    const { stdout } = await execFile(
      "git",
      ["-C", cwd, "diff", "--unified=0"],
      { timeout: 4000, maxBuffer: 1024 * 1024 },
    );
    return stdout.trim();
  } catch {
    return "";
  }
}

const CodeMemoryPlugin: Plugin = async ({ client, directory, worktree }) => {
  const cwd = worktree || directory || process.cwd();

  const log = (level: LogLevel, message: string): void => {
    // Fire-and-forget to avoid blocking session init.
    client.app
      .log({ body: { service: SERVICE, level, message } })
      .catch(() => {});
  };

  let memory: MemoryClient;
  try {
    memory = await createMemoryClient({ cwd, log });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    log("error", `failed to initialize memory client: ${msg}`);
    return {};
  }

  if (!memory.available) {
    log(
      "warn",
      "plugin loaded but `code-memory` CLI is missing. Install with " +
        "`pipx install git+https://github.com/fmflurry/code-memory` or expose " +
        "it via `uvx`. Hooks will no-op until then.",
    );
  }

  const stateBySession = new Map<string, SessionMemory>();
  // Set of sessions that have already kicked off the per-session delta
  // ingest (run once on the first prompt to catch out-of-band edits).
  const sessionsBootstrapped = new Set<string>();
  // Debounce handle for the resolver — coalesces bursts of write tools.
  let resolverTimer: NodeJS.Timeout | null = null;

  function scheduleResolver(): void {
    if (resolverTimer) clearTimeout(resolverTimer);
    resolverTimer = setTimeout(() => {
      resolverTimer = null;
      void memory.resolve().catch(() => {
        // resolve() already logs internally; swallow to keep the hook quiet
      });
    }, RESOLVER_DEBOUNCE_MS);
  }

  function getSession(id: string | undefined): SessionMemory | null {
    if (!id) return null;
    let s = stateBySession.get(id);
    if (!s) {
      s = {
        firstUserMessage: null,
        explicitMemorySeen: false,
        pendingGateNudge: false,
        pendingClaimNudge: null,
      };
      stateBySession.set(id, s);
    }
    return s;
  }

  return {
    "chat.message": async (input: ChatMessageInput, output: ChatMessageOutput) => {
      const sid = input.sessionID;
      const session = getSession(sid);
      if (!session) return;

      // New turn: only explicit MCP tool use satisfies the filesystem /
      // search / shell gate.
      session.explicitMemorySeen = false;

      const text = extractText(output.parts);
      if (text && !session.firstUserMessage) {
        session.firstUserMessage = text;
      }

      const claimHit = detectClaimIntent(text);
      if (claimHit) {
        session.pendingClaimNudge = formatClaimNudge(claimHit);
        log("info", `claim-intent: ${claimHit.kind} → "${claimHit.snippet}"`);
      }

      // Once per session, kick off a delta ingest in the background. Catches
      // out-of-band edits (vim, IDE, git pull) made between sessions so the
      // index isn't stale on the very first prompt. Also ensure a launchd /
      // systemd watcher unit exists for this repo so edits BETWEEN sessions
      // (when no agent is running) still trigger reingest automatically.
      if (memory.available && sid && !sessionsBootstrapped.has(sid)) {
        sessionsBootstrapped.add(sid);
        if (!isSafeProjectRoot(cwd)) {
          log(
            "warn",
            `code-memory: skipping bootstrap for unsafe project root "${cwd}" — ` +
              "must be a git repository that is not HOME or filesystem root. " +
              "Open OpenCode from inside a specific project directory.",
          );
        } else {
          memory.autostartInstallDetached();
          void memory.ingest().catch(() => {
            // ingest() logs internally; never block session start on failure.
          });
        }
      }
    },

    "experimental.chat.system.transform": async (
      input: ChatMessageInput,
      output: SystemTransformOutput,
    ) => {
      if (!Array.isArray(output.system)) return;
      const session = sessionLookup(stateBySession, input.sessionID);
      if (!session) return;

      // Drain a pending gate nudge from the previous turn (the agent ran a
      // shell/read tool without first hitting code-memory). The nudge is
      // one-shot — surfaced exactly once at the next turn's system prompt.
      if (session.pendingGateNudge) {
        session.pendingGateNudge = false;
        output.system.push(GATE_NUDGE);
      }

      // Drain a pending claim nudge. One-shot per turn: surface the
      // suggestion to call codememory_assert_claim, then clear it.
      if (session.pendingClaimNudge) {
        output.system.push(session.pendingClaimNudge);
        session.pendingClaimNudge = null;
      }
    },

    "tool.execute.before": async (input: ToolInput, _output: ToolOutput) => {
      const tool = (input.tool ?? "").toLowerCase();
      if (!GATED_READ_TOOLS.has(tool)) return;

      const session = sessionLookup(stateBySession, input.sessionID);
      if (!session || session.explicitMemorySeen || session.pendingGateNudge) return;

      // Soft nudge: never block. Flag the session so the next system
      // transform surfaces a one-shot reminder, and log a warning the
      // user sees in the OpenCode UI right away.
      session.pendingGateNudge = true;
      log(
        "warn",
        `gate: ${tool} called without explicit code-memory MCP use this turn — call codememory_retrieve first`,
      );
    },

    "tool.definition": async (
      input: ToolDefinitionInput,
      output: ToolDefinitionOutput,
    ) => {
      const tool = input.toolID.toLowerCase();
      if (!GATED_READ_TOOLS.has(tool)) return;

      const prefix =
        "For repo/code/docs orientation, call code-memory MCP first: " +
        "use codememory_retrieve before grep/glob/read/bash, then verify exhaustively. ";
      if (output.description.startsWith(prefix)) return;
      output.description = `${prefix}${output.description}`;
    },

    "tool.execute.after": async (input: ToolInput, output: ToolOutput) => {
      const tool = (input.tool ?? "").toLowerCase();

      // Any code-memory MCP call satisfies the gate for this turn.
      if (isMemoryTool(tool)) {
        const session = sessionLookup(stateBySession, input.sessionID);
        if (session) {
          session.explicitMemorySeen = true;
          session.pendingGateNudge = false;
        }
      }

      // Track filesystem reads for MCP efficiency metrics.
      // Records only when the agent used code-memory MCP this turn.
      if (GATED_READ_TOOLS.has(tool)) {
        const session = sessionLookup(stateBySession, input.sessionID);
        if (session?.explicitMemorySeen) {
          const path =
            pickToolPath(output.args) ?? pickToolPath(output.metadata) ?? "";
          void memory.recordRead(tool, path);
        }
      }

      if (!memory.available) return;
      if (!WRITE_TOOLS.has(tool)) return;
      if (!isSafeProjectRoot(cwd)) return;

      const path = pickToolPath(output.args) ?? pickToolPath(output.metadata);
      if (!path) return;

      // Guard: only reingest files that live inside the project root (cwd).
      // Resolving against cwd handles relative paths; the sep-suffix check
      // prevents false positives like /foo/bar matching the prefix of /foo/baz.
      const projectRoot = nodePath.resolve(cwd);
      const absPath = nodePath.resolve(cwd, path);
      if (absPath !== projectRoot && !absPath.startsWith(projectRoot + nodePath.sep)) {
        // File is outside the project — silently skip ingestion.
        return;
      }

      // 1. Re-ingest the single file (fast, background).
      void memory.reingest(path);

      // 2. Schedule the cross-file resolver. Debounced so a burst of edits
      //    (e.g. multi-file refactor) collapses to one resolver run after
      //    the dust settles.
      scheduleResolver();
    },

    event: async ({ event }: { event: EventEnvelope }) => {
      if (!memory.available) return;
      if (event.type !== "session.idle") return;

      const sid =
        typeof event.properties?.sessionID === "string"
          ? (event.properties.sessionID as string)
          : undefined;
      const session = sessionLookup(stateBySession, sid);
      if (!session?.firstUserMessage) return;

      const patch = await gitDiff(cwd);
      void memory.record({
        prompt: session.firstUserMessage,
        patch: patch || undefined,
        verdict: "idle",
      });

      // Claim extraction is NOT auto-fired here. Claims are authored
      // explicitly by the agent via `codememory_assert_claim` when it
      // judges a message claim-worthy.
    },
  };
};

function sessionLookup(
  map: Map<string, SessionMemory>,
  id: string | undefined,
): SessionMemory | null {
  if (!id) return null;
  return map.get(id) ?? null;
}

export default CodeMemoryPlugin;
