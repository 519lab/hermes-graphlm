"""On-disk maps, one folder per project, under the plugin's data directory.

Two layers per project:

* ``structure.json`` — the free map (tree, parser-proven import edges, import
  cycles). Rebuilt in a fraction of a second whenever files change.
* ``enriched.json`` — the LLM layer (module descriptions, file summaries, entry
  points, data flow). Expensive, so rebuilt rarely, and stamped with the commit
  it was generated against.

``map.json`` is what everything serves: the enrichment with its structural
fields replaced by the newest structure, so an edit-triggered rebuild never
throws away paid-for summaries. ``view.json`` is the drawable view for the
Desktop page and ``info.json`` the status line. The dashboard API reads these
files from a separate process, so every write is an atomic replace.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from graphlm.llm import CodebaseGraph
from graphlm.render import render_markdown
from graphlm.view import graph_view

from .repo import repo_key

STRUCTURE, ENRICHED = "structure", "enriched"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_graph(path: Path) -> CodebaseGraph | None:
    try:
        return CodebaseGraph.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def count_files(tree: str) -> int:
    """Files in graphlm's indented tree listing (directories end with ``/``)."""
    return sum(1 for line in tree.splitlines() if line.strip() and not line.endswith("/"))


def merge(structure: CodebaseGraph, enriched: CodebaseGraph | None) -> CodebaseGraph:
    """The served map: enrichment prose over the newest structure."""
    if enriched is None:
        return structure
    merged = enriched.model_copy(deep=True)
    merged.directory_tree = structure.directory_tree
    merged.deterministic_edges = structure.deterministic_edges
    merged.import_cycles = structure.import_cycles
    merged.meta = structure.meta
    return merged


class MapStore:
    """Reads and writes project maps under ``base`` (``…/plugin-data/graphlm/maps``)."""

    def __init__(self, base: Path) -> None:
        self.base = base

    def dir_for(self, root: Path) -> Path:
        return self.base / repo_key(root)

    # ---- reads -----------------------------------------------------------
    def load(self, root: Path) -> CodebaseGraph | None:
        return _read_graph(self.dir_for(root) / "map.json")

    def load_layer(self, root: Path, layer: str) -> CodebaseGraph | None:
        return _read_graph(self.dir_for(root) / f"{layer}.json")

    def info(self, root: Path) -> dict[str, Any] | None:
        return self._read_info(self.dir_for(root))

    def list_maps(self) -> list[dict[str, Any]]:
        if not self.base.is_dir():
            return []
        infos = [self._read_info(d) for d in self.base.iterdir() if d.is_dir()]
        return sorted((i for i in infos if i), key=lambda i: -i.get("updated_at", 0))

    # ---- writes ----------------------------------------------------------
    def save(self, root: Path, layer: str, graph: CodebaseGraph) -> tuple[CodebaseGraph, dict]:
        """Persist one layer, rebuild the served map, bump the version."""
        if layer not in (STRUCTURE, ENRICHED):
            raise ValueError(f"unknown layer {layer!r}")
        d = self.dir_for(root)
        _atomic_write(d / f"{layer}.json", graph.model_dump_json())
        structure = graph if layer == STRUCTURE else self.load_layer(root, STRUCTURE)
        enriched = graph if layer == ENRICHED else self.load_layer(root, ENRICHED)
        served = merge(structure or graph, enriched)
        _atomic_write(d / "map.json", served.model_dump_json())
        _atomic_write(d / "GRAPH.md", render_markdown(served))
        _atomic_write(d / "view.json", json.dumps(graph_view(served)))
        info = self._next_info(root, d, layer, graph, served, enriched)
        _atomic_write(d / "info.json", json.dumps(info))
        return served, info

    def record_error(self, root: Path, layer: str, message: str) -> dict[str, Any]:
        d = self.dir_for(root)
        info = self._read_info(d) or self._blank_info(root)
        info["error"] = {"layer": layer, "message": message[:500], "at": time.time()}
        info["status"] = "error"
        _atomic_write(d / "info.json", json.dumps(info))
        return info

    def set_status(self, root: Path, status: str) -> dict[str, Any]:
        d = self.dir_for(root)
        info = self._read_info(d) or self._blank_info(root)
        info["status"] = status
        info["status_at"] = time.time()
        _atomic_write(d / "info.json", json.dumps(info))
        return info

    # ---- info bookkeeping ------------------------------------------------
    @staticmethod
    def _read_info(d: Path) -> dict[str, Any] | None:
        try:
            data = json.loads((d / "info.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _blank_info(root: Path) -> dict[str, Any]:
        return {"key": repo_key(root), "root": str(root), "name": root.name,
                "version": 0, "status": "pending"}

    def _next_info(self, root: Path, d: Path, layer: str, graph: CodebaseGraph,
                   served: CodebaseGraph, enriched: CodebaseGraph | None) -> dict[str, Any]:
        prev = self._read_info(d) or self._blank_info(root)
        now = time.time()
        meta = served.meta
        info = {
            **self._blank_info(root),
            "version": int(prev.get("version", 0)) + 1,
            "status": "ready",
            "updated_at": now,
            "commit": meta.commit_sha if meta else None,
            "files": count_files(served.directory_tree),
            "ast_edges": len(served.deterministic_edges or []),
            "cycles": len(served.import_cycles),
            "modules": len(served.modules),
            "summaries": len(served.file_summaries),
            "enriched_at": prev.get("enriched_at"),
            "enriched_commit": prev.get("enriched_commit"),
            "enrich_usage": prev.get("enrich_usage"),
            "error": None,
        }
        if layer == ENRICHED and enriched is not None:
            info["enriched_at"] = now
            info["enriched_commit"] = graph.meta.commit_sha if graph.meta else None
            usage = graph.meta.usage if graph.meta else None
            info["enrich_usage"] = usage.model_dump() if usage else None
        return info
