#!/usr/bin/env python3
"""Inject the code-memory MCP block into an opencode.jsonc config.

Usage:
    add-mcp.py <path-to-opencode.jsonc>

Idempotent: re-running is a no-op if `code-memory` is already configured.
JSONC comments are stripped on write; a `.bak` copy is left next to the
original.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


MCP_BLOCK = {
    "type": "local",
    "command": [
        "uvx",
        "--from",
        "git+https://github.com/fmflurry/code-memory",
        "code-memory-mcp",
    ],
    "enabled": True,
    "environment": {"CODE_MEMORY_PROJECT": "auto"},
}


def strip_jsonc(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r'(^|[^:"\'])//[^\n]*', r"\1", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: add-mcp.py <opencode.jsonc>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])

    if path.exists():
        raw = path.read_text()
        try:
            data = json.loads(strip_jsonc(raw))
        except json.JSONDecodeError as exc:
            print(
                f"✗ failed to parse {path}: {exc}\n"
                "  add the MCP block manually (see README §MCP server).",
                file=sys.stderr,
            )
            return 1
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(raw)
        print(f"  backup: {backup}", file=sys.stderr)
    else:
        data = {}
        path.parent.mkdir(parents=True, exist_ok=True)

    if not isinstance(data, dict):
        print(f"✗ {path} is not a JSON object; cannot merge.", file=sys.stderr)
        return 1

    data.setdefault("$schema", "https://opencode.ai/config.json")
    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        print(f"✗ {path} has non-object `mcp` key; cannot merge.", file=sys.stderr)
        return 1

    if "code-memory" in mcp:
        print(f"✓ code-memory MCP already configured in {path}")
        return 0

    mcp["code-memory"] = MCP_BLOCK
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"✓ wrote MCP block to {path}")
    if path.suffix == ".jsonc":
        print(
            "  note: JSONC comments were not preserved; original is in the .bak file.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
