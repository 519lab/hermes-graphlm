"""What the agent is told about the map, and when.

Two delivery paths, both small:

* **System prompt section** (``register_system_prompt_section``): built once
  when a session starts and frozen, so it never breaks prompt caching. On the
  first visit to a project it builds the structural map synchronously (local
  parsing, typically well under a second) so even the first prompt has it.
* **pre_llm_call**: what the frozen section cannot carry — a later
  structural change worth knowing ("your edit introduced an import cycle"),
  LLM summaries arriving, or the full summary for a session whose section did
  not include it (e.g. the session moved into another repo). Each session
  hears about a given map version at most once.
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
                 ensure: Callable[[Path], bool]) -> None:
        self._store = store
        self._max_chars = max_chars
        self._ensure = ensure
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
        loaded = self._load(root)
        if loaded is None:
            return ""
        graph, info = loaded
        self._remember(_get(mapping, "session_id"), repo_key(root), int(info["version"]))
        return summarize(graph, info, self._max_chars)

    # ---- per turn ----------------------------------------------------------
    def for_turn(self, session_id: Optional[str], cwd: Optional[str]) -> Optional[str]:
        """Context to append to this turn's user message, or ``None``."""
        root = find_project_root(cwd)
        if root is None:
            return None
        loaded = self._load(root)
        if loaded is None:
            return None
        graph, info = loaded
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

    def _load(self, root: Path) -> Optional[tuple[CodebaseGraph, dict[str, Any]]]:
        """The stored map, building the structural one first if none exists.

        The first visit to a project must not race a background build: the
        session's first prompt is assembled milliseconds after it starts, and a
        one-shot run has no second turn. The structural map is local parsing
        bounded by ``max_files`` (typically well under a second), so ``ensure``
        builds it synchronously here; later rebuilds stay in the background.
        """
        graph, info = self._store.load(root), self._store.info(root)
        if graph is None or not info:
            if not self._ensure(root):
                return None
            graph, info = self._store.load(root), self._store.info(root)
            if graph is None or not info:
                return None
        return graph, info

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
