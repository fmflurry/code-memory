"""Removed — LLM-based claim extraction (Gemma/Ollama path).

This module previously contained ``ClaimExtractor``, ``Claim``, and
``ExtractionError`` for local-LLM claim extraction via Ollama. That
path has been removed in favor of agent-authored claims via
``codememory_assert_claim``, which gives the agent full control over
the triple and requires no external model dependency.

The shared infrastructure (``ClaimsStore``, ``ClaimRecord``,
``EntityResolver``, ``ClaimsIndexer``) lives in sibling modules and
continues to serve the agent-authored path.
"""
