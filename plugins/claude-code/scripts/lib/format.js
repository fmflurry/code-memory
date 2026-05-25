/**
 * Render a code-memory Context Pack into the Markdown block emitted as
 * `additionalContext` from the UserPromptSubmit hook. Mirrors the
 * OpenCode plugin formatter exactly so the agent sees the same shape.
 */

function formatPack(pack) {
  const lines = ["## code-memory Context Pack"];
  lines.push(`Query: ${pack.query}`);

  if (Array.isArray(pack.code) && pack.code.length > 0) {
    lines.push("", "### Code hits");
    for (const h of pack.code) {
      const loc = h.path ? `${h.path}:${h.start ?? "?"}-${h.end ?? "?"}` : "?";
      const kind = h.kind ?? "?";
      const name = h.name ?? "?";
      const score = typeof h.score === "number" ? h.score.toFixed(3) : "?";
      lines.push(`- ${loc} [${kind} ${name}] score=${score}`);
    }
  }

  if (Array.isArray(pack.episodes) && pack.episodes.length > 0) {
    lines.push("", "### Prior episodes");
    for (const ep of pack.episodes) {
      const verdict = ep.verdict ? ` (${ep.verdict})` : "";
      lines.push(`- ${ep.id}${verdict} :: ${ep.prompt}`);
    }
  }

  if (Array.isArray(pack.claims) && pack.claims.length > 0) {
    lines.push("", "### User claims");
    for (const c of pack.claims) {
      const neg = c.polarity === false ? " (NEGATED)" : "";
      const conf = typeof c.confidence === "number" ? c.confidence.toFixed(2) : "?";
      lines.push(`- ${c.subject} ${c.predicate} ${c.object}${neg} (conf=${conf})`);
    }
  }

  lines.push(
    "",
    "### Next-step tools (call these autonomously when applicable)",
    "",
    "Auto-injected Context Packs are orientation only. They do not replace an",
    "explicit code-memory MCP call when repo/code/docs orientation is needed.",
    "",
    "The Code hits above are **orientation only** — they do not answer topology",
    "questions. Before reading files, decide if a graph query would give you a",
    "precise answer in one call:",
    "",
    "**Docs / repo orientation:**",
    "",
    "- Docs inventory, repo documentation, or 'where do docs live?' → call",
    "  `codememory_retrieve` first, then `glob` / `read` to verify an exhaustive",
    "  list.",
    "",
    "- `codememory_callers(symbol)` — who calls this symbol? Use before rename/",
    "  refactor, or when asked 'what depends on X'.",
    "- `codememory_callees(symbol)` — what does the file defining this symbol",
    "  call? Use to map outgoing dependencies of a service/class.",
    "- `codememory_importers(target)` — which files import this module or path?",
    "  Use for 'who uses @scope/lib' or barrel-file impact analysis.",
    "- `codememory_dependencies(file)` — what does this file import? Use to",
    "  understand a file's external surface before reading it line-by-line.",
    "- `codememory_definitions(symbol)` — every file+line that defines a name.",
    "  Use first when a symbol name is ambiguous across the project.",
    "",
    "Default to one targeted graph call over a wide grep. Read source files",
    "only after the graph tells you exactly which lines to open.",
    "",
    "After completing a non-trivial task, call `codememory_record(prompt, plan,",
    "patch, verdict)` so future sessions can recall what worked.",
    "",
    "_Source: local code-memory index. Use as orientation; verify before acting._",
  );

  return lines.join("\n");
}

module.exports = { formatPack };
