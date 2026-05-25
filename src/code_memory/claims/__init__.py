"""User-prompt claim extraction (Graphiti-style).

The pipeline turns substantive user prompts into structured
``(subject, predicate, object)`` claims with bi-temporal validity so a
later session can answer "what did the user say about X last Tuesday?"
without re-reading every prompt.

Layout:
  * :mod:`.extractor` — local-LLM extraction (Ollama, gemma2:9b default).
  * :mod:`.store`     — SQLite store with bi-temporal columns and a
                        single-valued predicate registry for contradiction
                        handling.
"""

from .extractor import Claim, ClaimExtractor, ExtractionError
from .resolver import EntityRef, EntityResolver
from .store import ClaimRecord, ClaimsStore, SINGLE_VALUED_PREDICATES

__all__ = [
    "Claim",
    "ClaimExtractor",
    "ClaimRecord",
    "ClaimsStore",
    "EntityRef",
    "EntityResolver",
    "ExtractionError",
    "SINGLE_VALUED_PREDICATES",
]
