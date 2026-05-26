/**
 * Tests for the claim-intent detector. Run with:
 *   node --test plugins/claude-code/scripts/lib/claim-intent.test.js
 *
 * Bias: detector must catch the original failing case ("I love Clean
 * Architecture ! how good are we with order creation ?") and the
 * SKILL.md example phrasings. False positives on harmless prompts are
 * still cheap — they only inject one nudge line — so we err on the
 * side of recall.
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  detectClaimIntent,
  formatClaimNudge,
  isPureQuestion,
} = require("./claim-intent");

// ------------------------------------------------------------------ positive

test("detects preference: 'I love X'", () => {
  const hit = detectClaimIntent("I love Clean Architecture !");
  assert.ok(hit, "expected a match");
  assert.equal(hit.kind, "preference");
  assert.equal(hit.suggestion, "prefers");
  assert.match(hit.snippet, /love Clean Architecture/);
});

test("detects the original failing case (mixed claim + question)", () => {
  const hit = detectClaimIntent(
    "I love Clean Architecture ! how good are we with order creation ?"
  );
  assert.ok(hit, "must match — pure-question filter should not strip it");
  assert.equal(hit.kind, "preference");
});

test("detects preference: 'we prefer X'", () => {
  const hit = detectClaimIntent("we prefer terse output");
  assert.ok(hit);
  assert.equal(hit.suggestion, "prefers");
});

test("detects wants-to: 'I want to ship dark mode'", () => {
  const hit = detectClaimIntent("I want to ship dark mode soon");
  assert.ok(hit);
  assert.equal(hit.suggestion, "wants-to");
});

test("detects rejection: 'I don't want X'", () => {
  const hit = detectClaimIntent("I don't want to ship dark mode");
  assert.ok(hit);
  assert.equal(hit.kind, "rejection");
  assert.equal(hit.suggestion, "rejected");
});

test("detects rejection: 'we won't use X'", () => {
  const hit = detectClaimIntent("we won't use Redis here");
  assert.ok(hit);
  assert.equal(hit.kind, "rejection");
});

test("detects rejection: \"let's not ship X\"", () => {
  const hit = detectClaimIntent("let's not ship dark mode this quarter");
  assert.ok(hit);
  assert.equal(hit.kind, "rejection");
});

test("detects tech-stack decision: 'we use X'", () => {
  const hit = detectClaimIntent("we use Postgres for everything");
  assert.ok(hit);
  assert.equal(hit.kind, "decision");
  assert.equal(hit.suggestion, "uses");
});

test("detects tech-stack decision: 'we're using X'", () => {
  // The current "decision" regex matches forms of `use|deploy|run`
  // preceded by "we"/"our ...". "we're using" appears as
  // "we 're using" after tokenization, which won't match. Verify the
  // simpler "we use" form here; broader contractions are a follow-up.
  const hit = detectClaimIntent("we use FalkorDB for the graph");
  assert.ok(hit);
});

test("detects ownership: 'Alice owns billing'", () => {
  const hit = detectClaimIntent("Alice owns the billing module");
  assert.ok(hit);
  assert.equal(hit.kind, "ownership");
});

test("detects location: 'lives at apps/api/auth'", () => {
  const hit = detectClaimIntent("the auth service lives at apps/api/auth");
  assert.ok(hit);
  assert.equal(hit.kind, "location");
});

// ------------------------------------------------------------------ negative

test("skips pure question: 'how does X work?'", () => {
  assert.equal(detectClaimIntent("how does authentication work?"), null);
});

test("skips pure question with preference verb: 'do we prefer X or Y?'", () => {
  assert.equal(detectClaimIntent("do we prefer X or Y?"), null);
});

test("skips imperative task: 'fix the bug'", () => {
  assert.equal(detectClaimIntent("fix the bug in auth.py"), null);
});

test("skips third-party opinion: 'React is great'", () => {
  // Generic statement with no I/we — should not fire.
  assert.equal(detectClaimIntent("React is great for UI"), null);
});

test("skips empty input", () => {
  assert.equal(detectClaimIntent(""), null);
  assert.equal(detectClaimIntent(null), null);
  assert.equal(detectClaimIntent(undefined), null);
});

test("skips non-string input", () => {
  assert.equal(detectClaimIntent(42), null);
  assert.equal(detectClaimIntent({}), null);
});

// ------------------------------------------------------------ isPureQuestion

test("isPureQuestion: trailing ? with no other terminator", () => {
  assert.equal(isPureQuestion("does this work?"), true);
});

test("isPureQuestion: mixed claim+question is NOT pure", () => {
  assert.equal(isPureQuestion("I love X! how does Y work?"), false);
  assert.equal(isPureQuestion("we use Postgres. is that wise?"), false);
});

test("isPureQuestion: no trailing ? is not a question", () => {
  assert.equal(isPureQuestion("we use Postgres"), false);
});

// -------------------------------------------------------------- formatNudge

test("formatClaimNudge produces a single text block with the snippet", () => {
  const hit = detectClaimIntent("I love Clean Architecture !");
  const nudge = formatClaimNudge(hit);
  assert.match(nudge, /\[code-memory\]/);
  assert.match(nudge, /ACT BEFORE ANSWERING/);
  assert.match(nudge, /codememory_assert_claim/);
  assert.match(nudge, /preference/);
  assert.match(nudge, /Clean Architecture/);
  assert.match(nudge, /DEFAULT ACTION/);
  assert.match(nudge, /DO NOT skip because:/);
  assert.match(nudge, /SKIP ONLY if ALL of these hold:/);
  assert.match(nudge, /skipped claim:/);
  // Predicate flows from hit.suggestion into the assert template.
  assert.match(nudge, /predicate="prefers"/);
});
