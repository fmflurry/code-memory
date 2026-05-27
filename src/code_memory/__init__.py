from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("flurryx-code-memory")
except PackageNotFoundError:  # editable / source checkout without install
    __version__ = "0.0.0+local"
