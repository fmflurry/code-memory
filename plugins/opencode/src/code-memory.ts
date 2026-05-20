/**
 * code-memory OpenCode plugin
 *
 * Auto-retrieve: hook `chat.message` to detect substantive code intent, fetch
 * a Context Pack via `code-memory retrieve --json`, and stash it per session.
 * Hook `experimental.chat.system.transform` injects the pack into the system
 * prompt while it is fresh.
 *
 * Auto-learn: hook `tool.execute.after` to call `code-memory reingest <path>`
 * whenever the agent writes or edits a file. Hook `event session.idle` to
 * record the session as an episode (best-effort).
 *
 * All backend calls are best-effort: a missing or broken `code-memory` CLI
 * degrades to no-op without breaking the session.
 */

import { execFile as execFileCb } from "node:child_process";
import { promisify } from "node:util";

import type { Plugin } from "@opencode-ai/plugin";

import {
  type ContextPack,
  type LogLevel,
  type MemoryClient,
  createMemoryClient,
} from "./code-memory-lib/memory-client.ts";
import {
  extractQueryFromMessage,
  isSubstantiveCodeIntent,
} from "./code-memory-lib/intent.ts";

const execFile = promisify(execFileCb);

const PACK_TTL_MS = 5 * 60 * 1000; // 5 min
const DEDUP_WINDOW_MS = 60 * 1000; // 60 s
const WRITE_TOOLS: ReadonlySet<string> = new Set(["write", "edit", "patch"]);
const SERVICE = "code-memory";

interface SessionMemory {
  pack: ContextPack | null;
  query: string | null;
  fetchedAt: number;
  firstUserMessage: string | null;
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

function formatPack(pack: ContextPack): string {
  const lines: string[] = ["## code-memory Context Pack"];
  lines.push(`Query: ${pack.query}`);

  if (pack.code.length > 0) {
    lines.push("", "### Code hits");
    for (const h of pack.code) {
      const loc = h.path
        ? `${h.path}:${h.start ?? "?"}-${h.end ?? "?"}`
        : "?";
      const kind = h.kind ?? "?";
      const name = h.name ?? "?";
      lines.push(`- ${loc} [${kind} ${name}] score=${h.score.toFixed(3)}`);
    }
  }

  if (pack.episodes.length > 0) {
    lines.push("", "### Prior episodes");
    for (const ep of pack.episodes) {
      const verdict = ep.verdict ? ` (${ep.verdict})` : "";
      lines.push(`- ${ep.id}${verdict} :: ${ep.prompt}`);
    }
  }

  if (pack.graph.length > 0) {
    lines.push("", "### Graph neighbors");
    for (const n of pack.graph.slice(0, 12)) {
      lines.push(`- ${String(n.labels)} ${String(n.key)}`);
    }
  }

  lines.push(
    "",
    "_Source: local code-memory index. Use as orientation; verify before acting._",
  );
  return lines.join("\n");
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

  function getSession(id: string | undefined): SessionMemory | null {
    if (!id) return null;
    let s = stateBySession.get(id);
    if (!s) {
      s = { pack: null, query: null, fetchedAt: 0, firstUserMessage: null };
      stateBySession.set(id, s);
    }
    return s;
  }

  function pruneStaleSessions(now: number): void {
    for (const [id, s] of stateBySession.entries()) {
      if (s.fetchedAt && now - s.fetchedAt > PACK_TTL_MS * 4) {
        stateBySession.delete(id);
      }
    }
  }

  return {
    "chat.message": async (input: ChatMessageInput, output: ChatMessageOutput) => {
      const now = Date.now();
      pruneStaleSessions(now);

      const sid = input.sessionID;
      const session = getSession(sid);
      if (!session) return;

      const text = extractText(output.parts);
      if (text && !session.firstUserMessage) {
        session.firstUserMessage = text;
      }

      if (!memory.available || !isSubstantiveCodeIntent(text)) return;

      const query = extractQueryFromMessage(text);

      // Dedup the same query within DEDUP_WINDOW_MS.
      if (
        session.query === query &&
        session.fetchedAt &&
        now - session.fetchedAt < DEDUP_WINDOW_MS
      ) {
        return;
      }

      const pack = await memory.retrieve(query, { k: 8, eps: 5 });
      if (pack) {
        session.pack = pack;
        session.query = query;
        session.fetchedAt = Date.now();
        log(
          "info",
          `retrieved ${pack.code.length} code / ${pack.episodes.length} episodes for "${query.slice(0, 80)}"`,
        );
      }
    },

    "experimental.chat.system.transform": async (
      input: ChatMessageInput,
      output: SystemTransformOutput,
    ) => {
      if (!Array.isArray(output.system)) return;
      const session = sessionLookup(stateBySession, input.sessionID);
      if (!session || !session.pack) return;
      if (Date.now() - session.fetchedAt > PACK_TTL_MS) return;

      const isEmpty =
        session.pack.code.length === 0 &&
        session.pack.episodes.length === 0 &&
        session.pack.graph.length === 0;
      if (isEmpty) return;

      output.system.push(formatPack(session.pack));
    },

    "tool.execute.after": async (input: ToolInput, output: ToolOutput) => {
      if (!memory.available) return;
      const tool = (input.tool ?? "").toLowerCase();
      if (!WRITE_TOOLS.has(tool)) return;

      const path = pickToolPath(output.args) ?? pickToolPath(output.metadata);
      if (!path) return;

      // Fire-and-forget so the agent's turn isn't blocked on indexing.
      void memory.reingest(path);
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
