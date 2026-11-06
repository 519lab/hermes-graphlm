"""Which importers of a file actually use a given symbol.

The import graph answers "who imports ``tools/base.py``"; a refactor needs
"who uses ``ToolAdapter``". This narrows the graph's importer list to files
that mention the symbol as a whole word, with line numbers — the grep an
agent would otherwise run once per importer. Only files the graph already
named are read, each bounded in size, and never outside the project root.
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_FILES = 100
MAX_BYTES = 512 * 1024
MAX_LINES = 5
_IDENT = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")


def valid_symbol(symbol: str) -> bool:
    """An identifier (optionally dotted, e.g. ``Config.load``)."""
    return bool(_IDENT.match(symbol))


def find_references(root: Path, paths: list[str], symbol: str) -> dict[str, list[int]]:
    """``{path: [line, ...]}`` for each of ``paths`` that mentions ``symbol``.

    A file that cannot be read, is too large, or resolves outside ``root`` is
    skipped; a path with no mention maps to ``[]`` so callers can tell
    "imports but does not use" from "not checked".
    """
    name = symbol.rsplit(".", 1)[-1]
    pattern = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    root = root.resolve()
    found: dict[str, list[int]] = {}
    for rel in paths[:MAX_FILES]:
        try:
            path = (root / rel).resolve()
            path.relative_to(root)
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        lines = [i for i, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
        found[rel] = lines[:MAX_LINES]
    return found
