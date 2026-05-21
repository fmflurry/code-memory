/**
 * Heuristic: substantive code intent. Ported from intent.ts.
 * False positives = one extra retrieval; false negatives = blind agent.
 * Bias toward true.
 */

const MIN_LENGTH = 24;

const FOLLOWUP_TERMS = new Set([
  "yes", "no", "ok", "okay", "continue", "go", "proceed", "thanks",
  "thank you", "done", "stop", "wait", "pause", "next", "sure",
  "great", "perfect", "nice", "good",
]);

const CODE_VERBS = [
  /\b(refactor|implement|fix|debug|optimize|rewrite|extract|inline|rename|migrate|port|wire|hook|add|remove|delete|update|enable|disable|configure|test|review|design|build|deploy|trace)\b/i,
  /\b(why|how|where|what|which)\b.*\b(does|do|is|are|was|were|should|could|would|works?|fails?|breaks?|returns?|calls?|uses?|implements?)\b/i,
];

const CODE_SHAPED = [
  /[a-z][A-Za-z0-9]+\.[a-zA-Z][A-Za-z0-9]*\(/,
  /[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*/,
  /[a-z][A-Za-z0-9]+_[a-z][A-Za-z0-9_]*/,
  /\b\w+\/\w+/,
  /\.(ts|tsx|js|jsx|py|rs|go|java|kt|cs|cpp|c|h|hpp|rb|php|swift|sql|yml|yaml|toml|json)\b/i,
  /`[^`]+`/,
  /\b\w+::\w+\b/,
];

function isSubstantiveCodeIntent(text) {
  if (!text) return false;
  const trimmed = String(text).trim();
  if (trimmed.length === 0) return false;

  const lowered = trimmed.toLowerCase();
  if (FOLLOWUP_TERMS.has(lowered)) return false;

  if (trimmed.length < MIN_LENGTH) {
    return CODE_SHAPED.some((re) => re.test(trimmed));
  }

  if (CODE_SHAPED.some((re) => re.test(trimmed))) return true;
  if (CODE_VERBS.some((re) => re.test(trimmed))) return true;

  return trimmed.length >= 80;
}

function extractQueryFromMessage(text) {
  const collapsed = String(text || "").replace(/\s+/g, " ").trim();
  return collapsed.length > 480 ? collapsed.slice(0, 480) : collapsed;
}

module.exports = { isSubstantiveCodeIntent, extractQueryFromMessage };
