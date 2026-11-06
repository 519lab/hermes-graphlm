"""What the agent is told about the map, and when.

Two delivery paths, both small:

* **System prompt section** (``register_system_prompt_section``): built once
  when a session starts and frozen, so it never breaks prompt caching. It can
  only show a map that already exists at that moment.
* **pre_llm_call**: catches everything the section could not —
  the map becoming ready after the session started (first visit to a repo), a
  later structural change worth knowing ("your edit introduced an import
  cycle"), or LLM summaries arriving. Each session hears about a given map
  version at most once.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from graphlm.degree import top_fan_in
from graphlm.llm import CodebaseGraph

from .repo import find_project_root, repo_key
from .store import MapStore

TOOLS_HINT = (
    "Use graphlm_lookup(path) for what a file does and what it imports/is imported by, "
    "graphlm_impact(path) before changing shared code, graphlm_find(query) to locate code, "
    "and graphlm_overview for the whole picture — prefer these over broad searches."
)
MAX_SESSIONS = 512


def summarize(graph: CodebaseGraph, info: dict[str, Any], max_chars: int) -> str:
    """Compact, factual map summary for the agent (bounded to ``max_chars``)."""
    name = info.get("name") or "project"
    commit = (info.get("commit") or "")[:8] or "working tree"
    lines = [
        f"## Codebase map — {name} (graphlm)",
        f"{info.get('files', 0)} files · {info.get('ast_edges', 0)} parser-verified import "
        f"edges · {len(graph.import_cycles)} import cycles · map of {commit}.",
    ]
    if graph.modules or graph.file_summaries:
        lines.append(f"Module summaries available for {len(graph.modules)} modules / "
                     f"{len(graph.file_summaries)} files.")
    hubs = top_fan_in(graph, limit=6)
    if hubs:
        lines.append("Most imported: " + ", ".join(f"{p} ({n})" for p, n in hubs) + ".")
    if graph.entry_points:
        eps = sorted(graph.entry_points, key=lambda e: e.path)[:5]
        lines.append("Entry points: " + ", ".join(f"{e.path}::{e.name}" for e in eps) + ".")
    prod = [c for c in graph.import_cycles if not c.test_only][:3]
    for c in prod:
        lines.append("Import cycle: " + " → ".join([*c.nodes, c.nodes[0]]) + ".")
    for m in sorted(graph.modules, key=lambda m: m.path)[:12]:
        lines.append(f"- {m.path}: {m.description}")
    lines.append(TOOLS_HINT)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - len(TOOLS_HINT) - 2].rsplit("\n", 1)[0] + "\n" + TOOLS_HINT
    return text


@dataclass(slots=True)
class _Seen:
    key: str
    version: int


class ContextProvider:
    """Decides what map context each session gets, and remembers it."""

    def __init__(self, store: MapStore, *, max_chars: int,
                 on_missing: Callable[[Path], None]) -> None:
        self._store = store
        self._max_chars = max_chars
        self._on_missing = on_missing
        self._seen: "OrderedDict[str, _Seen]" = OrderedDict()
        self._notes: dict[str, list[tuple[int, str]]] = {}
        self._lock = threading.Lock()

    # ---- inputs from the builder ----------------------------------------
    def note(self, root: Path, version: int, message: str) -> None:
        """Queue a one-time message for sessions in ``root`` (e.g. new cycle)."""
        if not message:
            return
        with self._lock:
            notes = self._notes.setdefault(repo_key(root), [])
            notes.append((version, message))
            del notes[:-20]

    # ---- system prompt section -------------------------------------------
    def section(self, mapping: Any) -> str:
        root = find_project_root(_get(mapping, "cwd"))
        if root is None:
            return ""
        graph, info = self._store.load(root), self._store.info(root)
        if graph is None or not info:
            self._on_missing(root)
            return ""
        self._remember(_get(mapping, "session_id"), repo_key(root), int(info["version"]))
        return summarize(graph, info, self._max_chars)

    # ---- per turn ----------------------------------------------------------
    def for_turn(self, session_id: Optional[str], cwd: Optional[str]) -> Optional[str]:
        """Context to append to this turn's user message, or ``None``."""
        root = find_project_root(cwd)
        if root is None:
            return None
        info = self._store.info(root)
        graph = self._store.load(root) if info else None
        if graph is None or not info:
            self._on_missing(root)
            return None
        key, version = repo_key(root), int(info.get("version", 0))
        with self._lock:
            seen = self._seen.get(session_id or "")
        if seen is None or seen.key != key:
            self._remember(session_id, key, version)
            return summarize(graph, info, self._max_chars)
        if version <= seen.version:
            return None
        with self._lock:
            fresh = [m for v, m in self._notes.get(key, []) if v > seen.version]
        self._remember(session_id, key, version)
        if not fresh:
            return None
        return "graphlm map update: " + " ".join(dict.fromkeys(fresh))

    def _remember(self, session_id: Optional[str], key: str, version: int) -> None:
        if not session_id:
            return
        with self._lock:
            self._seen[session_id] = _Seen(key, version)
            self._seen.move_to_end(session_id)
            while len(self._seen) > MAX_SESSIONS:
                self._seen.popitem(last=False)


def _get(mapping: Any, key: str) -> Any:
    try:
        return mapping.get(key)
    except AttributeError:
        return getattr(mapping, key, None)
