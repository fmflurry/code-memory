"""Resolution of ``embed_dim`` from a model name.

The footgun this guards: operator swaps ``EMBED_MODEL`` from
``bge-m3`` (1024-d) to ``nomic-embed-text`` (768-d) without also
setting ``EMBED_DIM``. Qdrant collection gets recreated with the
wrong width, every upsert silently truncates or rejects. The known-
model table fixes that by giving sensible defaults; these tests pin
the table + override behaviour.
"""

from __future__ import annotations

import pytest

from code_memory.config import resolve_embed_dim


@pytest.mark.parametrize(
    "model, expected",
    [
        ("bge-m3", 1024),
        ("BGE-M3", 1024),
        ("bge-m3:latest", 1024),
        ("bge-m3:567m-fp16", 1024),
        ("BAAI/bge-m3", 1024),
        ("nomic-embed-text", 768),
        ("nomic-embed-text:latest", 768),
        ("nomic-embed-text-v1.5", 768),
        ("bge-small-en", 384),
        ("bge-base-en", 768),
        ("mxbai-embed-large", 1024),
        ("snowflake-arctic-embed:s", 384),
        ("snowflake-arctic-embed:m", 768),
        ("snowflake-arctic-embed:l", 1024),
    ],
)
def test_known_model_dims(model: str, expected: int) -> None:
    assert resolve_embed_dim(model) == expected


def test_explicit_override_wins(capsys: pytest.CaptureFixture[str]) -> None:
    """Operator passing ``EMBED_DIM`` keeps full control."""
    assert resolve_embed_dim("bge-m3", override=2048) == 2048
    # No warning on explicit override.
    assert "WARNING" not in capsys.readouterr().err


def test_unknown_model_warns_and_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Custom model without override: fall back to 1024 with a stderr warning."""
    dim = resolve_embed_dim("my-private-embedder")
    assert dim == 1024
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "my-private-embedder" in captured.err
    assert "EMBED_DIM" in captured.err


def test_zero_override_treated_as_auto() -> None:
    """``override=0`` is the sentinel value, not an override."""
    assert resolve_embed_dim("bge-m3", override=0) == 1024
