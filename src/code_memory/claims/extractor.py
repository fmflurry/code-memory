"""Local-LLM claim extractor.

Calls an Ollama-served instruct model (gemma2:9b by default) in JSON
mode and returns a list of :class:`Claim` records. Output is validated
defensively because LLMs lie:

  * ``evidence_span`` must be a literal substring of the source prompt.
    Hallucinated triples that paraphrase the input are dropped.
  * ``confidence`` below ``CLAIMS_MIN_CONFIDENCE`` is dropped.
  * Empty / non-string subject or object is dropped.

The extractor never raises on a malformed model response — it returns
an empty list so the caller (an async hook) never blocks the session.
The only raised exception is :class:`ExtractionError` for hard
infrastructure failures (Ollama unreachable, model not pulled).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import CONFIG

_LOG = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    """Raised when the LLM backend itself is unreachable or misconfigured."""


@dataclass(frozen=True)
class Claim:
    subject: str
    predicate: str
    object: str
    polarity: bool  # True = asserts, False = negates ("does not use X")
    confidence: float
    evidence_span: str


# JSON schema embedded in the prompt. Ollama's structured-output mode
# uses this verbatim to constrain the decoder. The schema is intentionally
# narrow — predicates are normalized to kebab-case verbs so downstream
# resolution doesn't have to disambiguate "uses" / "USES" / "Uses".
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "minLength": 1},
                    "predicate": {"type": "string", "minLength": 1},
                    "object": {"type": "string", "minLength": 1},
                    "polarity": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_span": {"type": "string", "minLength": 1},
                },
                "required": [
                    "subject",
                    "predicate",
                    "object",
                    "polarity",
                    "confidence",
                    "evidence_span",
                ],
            },
        }
    },
    "required": ["claims"],
}


_SYSTEM_PROMPT = """\
You extract factual claims from a software engineer's chat message.

Output JSON only, matching the provided schema. Each claim is a
(subject, predicate, object) triple plus polarity, confidence, and an
``evidence_span`` that is a verbatim substring of the input.

Rules:
- Predicate is kebab-case verb phrase: "uses", "prefers", "is-located-at",
  "depends-on", "wants-to", "rejected", "owns", "is-a", "deployed-to".
- Subject and object are short noun phrases lifted from the message;
  normalize case but keep technical identifiers as written.
- Skip questions, hypotheticals, opinions about third parties, and
  small talk. Only extract assertions the user is making about their
  project, tooling, preferences, or themselves.
- ``confidence`` reflects how certain you are this is an assertion (not
  a question or speculation), not how true the claim is.
- If nothing assertive, return {"claims": []}.

Examples:

INPUT: "we use Qdrant for vectors and FalkorDB for the graph"
OUTPUT: {"claims": [
  {"subject":"project","predicate":"uses","object":"Qdrant",
   "polarity":true,"confidence":0.95,"evidence_span":"use Qdrant for vectors"},
  {"subject":"project","predicate":"uses","object":"FalkorDB",
   "polarity":true,"confidence":0.95,"evidence_span":"FalkorDB for the graph"}
]}

INPUT: "should I use Redis here?"
OUTPUT: {"claims": []}

INPUT: "I don't want to ship dark mode"
OUTPUT: {"claims": [
  {"subject":"user","predicate":"rejected","object":"dark mode",
   "polarity":true,"confidence":0.9,"evidence_span":"don't want to ship dark mode"}
]}
"""


class ClaimExtractor:
    """Thin sync wrapper over Ollama's /api/chat with JSON-mode output.

    Construction is cheap; the HTTP client is created lazily so import
    of this module never touches the network.
    """

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        min_confidence: float | None = None,
    ) -> None:
        self.url = (url or CONFIG.ollama_url).rstrip("/")
        self.model = model or CONFIG.claims_llm_model
        self.timeout = timeout if timeout is not None else CONFIG.claims_llm_timeout
        self.min_confidence = (
            min_confidence
            if min_confidence is not None
            else CONFIG.claims_min_confidence
        )
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------ http

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> ClaimExtractor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------------- extract

    def extract(self, prompt: str) -> list[Claim]:
        """Run extraction over a single user prompt.

        Returns the validated, deduplicated, confidence-filtered list.
        Never raises on a malformed model response — returns ``[]``.
        Raises :class:`ExtractionError` only on transport-level failures.
        """
        prompt = prompt.strip()
        if not prompt:
            return []

        try:
            raw = self._call_ollama(prompt)
        except httpx.HTTPError as exc:
            raise ExtractionError(f"Ollama call failed: {exc}") from exc

        return self._parse_and_validate(raw, prompt)

    # ------------------------------------------------------------ internals

    def _call_ollama(self, prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "format": _OUTPUT_SCHEMA,
            "stream": False,
            "options": {"temperature": 0.0},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        res = self._http().post(f"{self.url}/api/chat", json=payload)
        res.raise_for_status()
        data = res.json()
        msg = data.get("message") or {}
        return str(msg.get("content") or "")

    def _parse_and_validate(self, raw: str, source_prompt: str) -> list[Claim]:
        if not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            _LOG.warning("claim extractor: non-JSON response, dropping")
            return []

        items = parsed.get("claims")
        if not isinstance(items, list):
            return []

        out: list[Claim] = []
        seen: set[tuple[str, str, str, bool]] = set()
        for item in items:
            claim = self._coerce(item, source_prompt)
            if claim is None:
                continue
            key = (
                claim.subject.lower(),
                claim.predicate.lower(),
                claim.object.lower(),
                claim.polarity,
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(claim)
        return out

    def _coerce(self, item: Any, source_prompt: str) -> Claim | None:
        if not isinstance(item, dict):
            return None
        try:
            subject = str(item["subject"]).strip()
            predicate = str(item["predicate"]).strip().lower().replace(" ", "-")
            obj = str(item["object"]).strip()
            polarity = bool(item["polarity"])
            confidence = float(item["confidence"])
            evidence = str(item["evidence_span"]).strip()
        except (KeyError, TypeError, ValueError):
            return None

        if not subject or not predicate or not obj or not evidence:
            return None
        if confidence < self.min_confidence:
            return None
        # Anti-hallucination: evidence must be present in the source.
        if evidence.lower() not in source_prompt.lower():
            _LOG.debug(
                "claim extractor: dropping hallucinated span %r", evidence
            )
            return None

        return Claim(
            subject=subject,
            predicate=predicate,
            object=obj,
            polarity=polarity,
            confidence=confidence,
            evidence_span=evidence,
        )
