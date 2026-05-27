from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Two dist names exist in the wild:
#   * "flurryx-code-memory" — current PyPI name (>= 0.4.0)
#   * "code-memory" — legacy name used by uv-tool installs from git+main
#     before the rename. Falls back to keep the older installs reporting
#     a real version until they run `code-memory update --bleeding`.
__version__ = "0.0.0+local"
for _name in ("flurryx-code-memory", "code-memory"):
    try:
        __version__ = _pkg_version(_name)
        break
    except PackageNotFoundError:
        continue
