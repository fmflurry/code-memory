"""User-prompt claims.

The ``codememory_assert_claim`` tool lets agents author structured
``(subject, predicate, object)`` claims with bi-temporal validity so a
later session can answer "what did the user say about X last Tuesday?"
without re-reading every prompt.

Layout:
  * :mod:`.store`     — SQLite store with bi-temporal columns and a
                        single-valued predicate registry for contradiction
                        handling.
  * :mod:`.resolver`  — entity resolution against the vector index.
  * :mod:`.indexer`   — batch indexing of stored claims.
"""

from .indexer import ClaimsIndexer, make_claims_indexer
from .resolver import EntityRef, EntityResolver
from .store import ClaimRecord, ClaimsStore, SINGLE_VALUED_PREDICATES, UpsertResult

__all__ = [
    "ClaimRecord",
    "ClaimsIndexer",
    "ClaimsStore",
    "EntityRef",
    "EntityResolver",
    "SINGLE_VALUED_PREDICATES",
    "UpsertResult",
    "make_claims_indexer",
]
