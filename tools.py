"""Model-callable map tools — lookups over the stored map, no LLM calls.

Only ``graphlm_impact`` with ``symbol`` reads files: the importers the map
named, bounded (see ``references.py``).

Each tool answers from the stored map of the session's project (or an
explicit ``project`` path). When no map exists yet, the free structural map is
built on the spot (well under a second on typical repos) so the first call
never comes back empty-handed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from graphlm import query

from .builder import MapBuilder
from .references import find_references, valid_symbol
from .repo import find_project_root
from .store import MapStore

TOOLSET = "graphlm"

_PROJECT = {
    "type": "string",
    "description": "Project directory. Defaults to the session's working directory.",
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "graphlm_overview": {
        "name": "graphlm_overview",
        "description": (
            "Codebase map overview for the current project: file/edge/cycle counts, the most "
            "imported files (hubs), entry points, architecture notes and map freshness. Call "
            "this first when orienting in an unfamiliar repository."
        ),
        "parameters": {"type": "object", "properties": {"project": _PROJECT}},
    },
    "graphlm_lookup": {
        "name": "graphlm_lookup",
        "description": (
            "Everything the map knows about one file: its summary and symbols (when "
            "enriched), what it imports, what imports it, and whether it sits in an import "
            "cycle. Accepts a full path, a path suffix, or a unique name like 'config'."
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "project": _PROJECT},
            "required": ["path"],
        },
    },
    "graphlm_find": {
        "name": "graphlm_find",
        "description": (
            "Find files by keyword across paths, module names, summaries and symbols. Use it "
            "to locate where something lives before grepping."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "project": _PROJECT,
            },
            "required": ["query"],
        },
    },
    "graphlm_impact": {
        "name": "graphlm_impact",
        "description": (
            "Blast radius of changing a file: every file that imports it, directly or "
            "(transitive=true) through other files, with distance. Pass symbol (a function, "
            "class or constant name) to also get which of those files actually use it, with "
            "line numbers — no separate grep needed. Call before editing shared code or "
            "changing a signature."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "transitive": {"type": "boolean"},
                "symbol": {"type": "string",
                           "description": "Identifier to look for in the importing files."},
                "project": _PROJECT,
            },
            "required": ["path"],
        },
    },
    "graphlm_cycles": {
        "name": "graphlm_cycles",
        "description": "Import cycles in the project, with members and risk scores.",
        "parameters": {"type": "object", "properties": {"project": _PROJECT}},
    },
}


def _err(message: str) -> str:
    return json.dumps({"error": message})


class MapTools:
    """Tool handlers bound to one store/builder and a session-cwd source."""

    def __init__(self, store: MapStore, builder: MapBuilder,
                 cwd: Callable[[], Optional[str]]) -> None:
        self._store = store
        self._builder = builder
        self._cwd = cwd

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {
            "graphlm_overview": self.overview,
            "graphlm_lookup": self.lookup,
            "graphlm_find": self.find,
            "graphlm_impact": self.impact,
            "graphlm_cycles": self.cycles,
        }

    # ---- handlers (args dict in, JSON string out, never raise) -----------
    def overview(self, args: dict[str, Any], **_: Any) -> str:
        return self._answer(args, lambda idx, root: query.overview(idx))

    def lookup(self, args: dict[str, Any], **_: Any) -> str:
        path = str(args.get("path") or "").strip()
        if not path:
            return _err("path is required")
        return self._answer(args, lambda idx, root: query.module_info(idx, path))

    def find(self, args: dict[str, Any], **_: Any) -> str:
        text = str(args.get("query") or "").strip()
        if not text:
            return _err("query is required")
        limit = _clamp(args.get("limit"), 15, 1, 50)
        return self._answer(args, lambda idx, root: query.find(idx, text, limit=limit))

    def impact(self, args: dict[str, Any], **_: Any) -> str:
        path = str(args.get("path") or "").strip()
        if not path:
            return _err("path is required")
        transitive = bool(args.get("transitive", False))
        symbol = str(args.get("symbol") or "").strip()
        if symbol and not valid_symbol(symbol):
            return _err("symbol must be an identifier, e.g. ToolAdapter or Config.load")

        def run(idx: query.MapIndex, root: Path) -> Any:
            result = query.dependents(idx, path, transitive=transitive, limit=100)
            if symbol and result.get("found"):
                importers = [d["path"] for d in result["dependents"]]
                refs = find_references(root, importers, symbol)
                result["symbol"] = symbol
                result["uses_symbol"] = [{"path": p, "lines": refs[p]} for p in importers
                                         if refs.get(p)]
                result["imports_without_using"] = [p for p in importers if refs.get(p) == []]
            return result

        return self._answer(args, run)

    def cycles(self, args: dict[str, Any], **_: Any) -> str:
        return self._answer(args, lambda idx, root: query.cycles(idx))

    # ---- shared plumbing ---------------------------------------------------
    def _answer(self, args: dict[str, Any], fn: Callable[[query.MapIndex, Path], Any]) -> str:
        root = find_project_root(args.get("project") or self._cwd())
        if root is None:
            return _err("not inside a project (no git repository or project manifest found)")
        try:
            graph = self._store.load(root)
            if graph is None:
                event = self._builder.build_now(root)
                if event.error:
                    return _err(f"could not map {root}: {event.error}")
                graph = self._store.load(root)
            if graph is None:
                return _err(f"no map available for {root}")
            index = query.build_index(graph)
            payload = {"result": fn(index, root), "map": self._map_status(index, root)}
        except Exception as exc:  # a tool must return an error, not raise
            return _err(f"{type(exc).__name__}: {exc}")
        return json.dumps(payload, default=str)

    def _map_status(self, index: query.MapIndex, root: Path) -> dict[str, Any]:
        info = self._store.info(root) or {}
        stale = query.staleness(index, root)
        return {
            "project": str(root),
            "freshness": stale["state"],
            "summaries": "available" if info.get("enriched_at") else "not yet generated",
        }


def _clamp(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default
