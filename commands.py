"""``/graphlm`` in a session and ``hermes graphlm`` on the command line."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .builder import MapBuilder
from .repo import find_project_root
from .store import STRUCTURE, MapStore

STALE_ENRICH_S = 3_900  # two passes at the max llm_timeout (1800 s), plus margin

USAGE = (
    "/graphlm [status|refresh|enrich|path]\n"
    "  status   map state for this project (default)\n"
    "  refresh  rebuild the structural map now (free, local)\n"
    "  enrich   (re)generate module summaries with the configured model (uses tokens)\n"
    "  path     where the map files live (GRAPH.md is readable by the agent)"
)


def describe(info: Optional[dict[str, Any]]) -> str:
    """One status block for a project's map."""
    if not info:
        return "No map yet. It is built automatically on the next turn, or run /graphlm refresh."
    commit = (info.get("commit") or "")[:8] or "working tree"
    status = info.get("status")
    if status == "enriching" and time.time() - float(info.get("status_at") or 0) > STALE_ENRICH_S:
        status = "enrichment interrupted (the process exited) — /graphlm enrich to retry"
    lines = [
        f"graphlm map — {info.get('name')} ({info.get('root')})",
        f"  status: {status} · version {info.get('version')} · map of {commit}",
        f"  {info.get('files', 0)} files · {info.get('ast_edges', 0)} import edges · "
        f"{info.get('cycles', 0)} cycles",
    ]
    if info.get("enriched_at"):
        ec = (info.get("enriched_commit") or "")[:8] or "working tree"
        lines.append(f"  summaries: {info.get('modules', 0)} modules / "
                     f"{info.get('summaries', 0)} files (generated at {ec})")
    else:
        lines.append("  summaries: not generated (/graphlm enrich)")
    if info.get("error"):
        lines.append(f"  last error ({info['error'].get('layer')}): {info['error'].get('message')}")
    return "\n".join(lines)


class Commands:
    """Slash and CLI handlers sharing one store/builder."""

    def __init__(self, store: MapStore, builder: MapBuilder,
                 cwd: Callable[[], Optional[str]]) -> None:
        self._store = store
        self._builder = builder
        self._cwd = cwd

    def slash(self, raw_args: str = "", **_: Any) -> str:
        action = (raw_args or "status").strip().split()[0].lower() if raw_args.strip() else "status"
        root = find_project_root(self._cwd())
        if root is None:
            return "graphlm: this session is not inside a project (no git repo or manifest)."
        if action == "status":
            return describe(self._store.info(root))
        if action == "refresh":
            event = self._builder.build_now(root, STRUCTURE)
            return f"graphlm: map rebuild failed — {event.error}" if event.error else describe(event.info)
        if action == "enrich":
            self._builder.request(root, enrich=True)
            return ("graphlm: generating module summaries in the background with your "
                    "configured model; the map updates when it finishes.")
        if action == "path":
            return str(self._store.dir_for(root) / "GRAPH.md")
        return USAGE

    # ---- CLI -----------------------------------------------------------------
    @staticmethod
    def setup_cli(parser: argparse.ArgumentParser) -> None:
        sub = parser.add_subparsers(dest="graphlm_action")
        build = sub.add_parser("map", help="Build the structural map for a project now")
        build.add_argument("path", nargs="?", default=".")
        status = sub.add_parser("status", help="Show map status for a project")
        status.add_argument("path", nargs="?", default=".")
        sub.add_parser("list", help="List every mapped project")

    def run_cli(self, args: argparse.Namespace) -> int:
        action = getattr(args, "graphlm_action", None) or "list"
        if action == "list":
            maps = self._store.list_maps()
            if not maps:
                print("No projects mapped yet.")
            for info in maps:
                print(describe(info))
            return 0
        root = find_project_root(Path(getattr(args, "path", ".")).resolve())
        if root is None:
            print("Not inside a project (no git repository or project manifest found).")
            return 1
        if action == "map":
            event = self._builder.build_now(root, STRUCTURE)
            print(f"graphlm: failed — {event.error}" if event.error else describe(event.info))
            return 1 if event.error else 0
        print(describe(self._store.info(root)))
        return 0
