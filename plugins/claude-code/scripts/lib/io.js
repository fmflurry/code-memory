/**
 * Hook IO helpers. Claude Code invokes a hook as a short-lived process,
 * passes the event JSON on stdin, and reads stdout for either:
 *   - plain text (appended as context for UserPromptSubmit/SessionStart), or
 *   - a JSON object with hook-specific fields (preferred).
 *
 * We standardize on the JSON envelope so each hook can also emit
 * `systemMessage` strings for diagnostics without leaking into context.
 */

function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => (data += chunk));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", () => resolve(data));
    // Some shells close stdin immediately on empty input.
    if (process.stdin.isTTY) resolve(data);
  });
}

async function readEvent() {
  const raw = await readStdin();
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

function emitJson(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function done(obj) {
  if (obj) emitJson(obj);
  process.exit(0);
}

module.exports = { readEvent, emitJson, done };
