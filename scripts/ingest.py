"""Convenience script: `python scripts/ingest.py /path/to/repo`."""

from __future__ import annotations

import sys
from pathlib import Path

from code_memory.orchestrator import Pipeline


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: ingest.py <repo_path>")
        sys.exit(2)
    root = Path(sys.argv[1])
    pipe = Pipeline()
    stats = pipe.ingest_repo(root)
    print(stats)


if __name__ == "__main__":
    main()
