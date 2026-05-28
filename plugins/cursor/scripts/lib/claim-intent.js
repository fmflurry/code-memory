/**
 * Heuristic: does this user message contain a durable assertion that
 * the agent should consider asserting via codememory_assert_claim?
 *
 * Bias: false positives (one extra reminder line in context) are cheap;
 * false negatives (silent claim drop) are the bug we're fixing. So the
 * patterns are deliberately broad — the *agent* makes the final
 * decision; we only nudge it to think about it.
 *
 * Returns one of:
 *   - null                            → no signal, stay silent
 *   - { kind, snippet, suggestion }   → matched, emit nudge
 *
 * `kind` is one of: "preference", "decision", "rejection", "ownership",
 * "location". `suggestion` is a hint at the predicate the agent should
 * use, NOT a prescribed triple — the agent picks the final subject /
 * object based on full context.
 */

/**
 * Preference verbs in the first person. We require the verb to follow
 * an "I" or "we" pronoun so generic statements about third parties
 * ("React is great") don't trigger.
 *
 * Each pattern captures the snippet that justified the match so the
 * nudge can quote it back to the agent.
 */
const PATTERNS = [
  {
    kind: "preference",
    re: /\b(i|we)\s+(love|like|prefer|enjoy|favor|favour)\b[^.!?\n]{1,120}/i,
    suggestion: "prefers",
  },
  {
    kind: "preference",
    re: /\b(i|we)\s+(want|need|wanna|wish|would\s+like)\s+to\b[^.!?\n]{1,120}/i,
    suggestion: "wants-to",
  },
  {
    kind: "rejection",
    re: /\b(i|we)\s+(hate|dislike|reject|refuse|don'?t\s+(want|like|use)|won'?t\s+(use|ship|build))\b[^.!?\n]{1,120}/i,
    suggestion: "rejected",
  },
  {
    kind: "rejection",
    // "let's not …" / "we're not doing …"
    re: /\b(let'?s\s+not|we'?re\s+not\s+(using|doing|shipping|building))\b[^.!?\n]{1,120}/i,
    suggestion: "rejected",
  },
  {
    kind: "decision",
    // Tech-stack assertions: "we use Postgres", "we're using …", "we deploy to …"
    re: /\b(we|our\s+(project|team|app|service))\s+(use|uses|using|deploy|deploys|deployed|run|runs|running)\b[^.!?\n]{1,120}/i,
    suggestion: "uses",
  },
  {
    kind: "ownership",
    re: /\b([A-Z][a-zA-Z]+|i|we)\s+own[s]?\b[^.!?\n]{1,120}/i,
    suggestion: "owns",
  },
  {
    kind: "location",
    re: /\b(lives?|located|sits?|is)\s+(at|in|under)\s+[`"']?[a-z0-9_\-./]+[`"']?/i,
    suggestion: "is-located-at",
  },
];

/**
 * Pure-question filter. If the entire message is a question the user
 * is asking us, no claim — even if a preference verb shows up inside
 * ("do we prefer X or Y?"). We detect this by checking whether the
 * trimmed message ends with `?` AND has no declarative clause before
 * an interrogative one.
 */
function isPureQuestion(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed.endsWith("?")) return false;
  // Mixed prompts ("I love X! how does Y work?") have a clause
  // terminator before the question — those still count as containing
  // a claim. Treat the message as pure-question only when there's no
  // `.` or `!` before the trailing `?`.
  const stripped = trimmed.slice(0, -1);
  return !/[.!]/.test(stripped);
}

function detectClaimIntent(text) {
  if (!text || typeof text !== "string") return null;
  if (isPureQuestion(text)) return null;

  for (const { kind, re, suggestion } of PATTERNS) {
    const m = re.exec(text);
    if (m) {
      const snippet = String(m[0]).trim().replace(/\s+/g, " ");
      return { kind, snippet, suggestion };
    }
  }
  return null;
}

/**
 * Format the additionalContext block injected before the agent sees
 * the user's message.
 *
 * Polarity is deliberately flipped: the previous wording ("If this is a
 * stable preference, call …") made *skipping* the default — the agent
 * could rationalize a no-op for any borderline case. The new wording
 * makes *asserting* the default, encloses the loopholes the agent has
 * historically used to avoid the call, and demands a one-line written
 * justification on skip so silent drops surface in the transcript.
 */
function formatClaimNudge(hit) {
  return [
    "[code-memory] Durable user assertion detected — ACT BEFORE ANSWERING.",
    "",
    `Matched (${hit.kind}): "${hit.snippet}"`,
    `Suggested triple: user ${hit.suggestion} "<extracted object>"`,
    "",
    "DEFAULT ACTION: call codememory_assert_claim NOW, in the same response,",
    "BEFORE any other tool call or user-facing text.",
    "",
    "  codememory_assert_claim(",
    '    subject="user",',
    `    predicate="${hit.suggestion}",`,
    '    object="<extracted object>",',
    '    project="<project slug>",',
    '    evidence_span="<verbatim user quote>"',
    "  )",
    "",
    "DO NOT skip because:",
    "  - the fact is already in CLAUDE.md / AGENTS.md / code  (restatement",
    "    reinforces; assert with confidence=0.85)",
    '  - the wording is emotional ("love", "hate", "really want")  (sentiment',
    "    verbs ARE preference signals when paired with a noun/tech/pattern)",
    "  - the user is also asking a question in the same message  (handle BOTH:",
    "    assert first, then answer)",
    '  - you "are not sure of the scope"  (assert with the literal object the',
    "    user named; refine later if contradicted)",
    "",
    "SKIP ONLY if ALL of these hold:",
    '  - the sentence is hypothetical ("if we used X..."), counterfactual,',
    "    or a quoted third party",
    "  - OR the user is asking whether they should adopt X (question, not",
    "    assertion)",
    "  - OR the user explicitly retracts it in the same message",
    "  - OR a higher-confidence claim with the same subject+predicate+object",
    "    was asserted in this session (dedupe)",
    "",
    'If you skip, state ONE LINE in your response: "skipped claim: <reason>".',
    "Silent skips are a bug.",
  ].join("\n");
}

module.exports = {
  detectClaimIntent,
  formatClaimNudge,
  isPureQuestion,
  PATTERNS,
};
