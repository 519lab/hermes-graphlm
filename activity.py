"""What files the agent is touching, from its tool calls.

``post_tool_call`` hands us the tool name, its arguments and its status.
``read_file`` / ``write_file`` / replace-mode ``patch`` name the file in
``path``; a V4A ``patch`` names files only in its ``*** Update File:`` style
headers, so those are parsed. Terminal commands are not interpreted — a
shell line is not a reliable record of which files it touched.

Touches are kept per project in a bounded JSONL log under the plugin's data
directory (the Desktop page reads it from another process) and returned to the
caller so it can broadcast them live.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

READ, WRITE = "read", "write"
_TOOL_KIND = {"read_file": READ, "write_file": WRITE, "patch": WRITE}
_V4A_HEADER = re.compile(
    r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$|^\*\*\* Move File: (.+?) -> (.+?)\s*$",
    re.MULTILINE,
)
MAX_LOG_LINES = 2_000


@dataclass(frozen=True, slots=True)
class Touch:
    """One file the agent read or wrote, as a path relative to the project."""

    path: str
    kind: str
    tool: str
    session_id: str
    ts: float


def paths_from_tool(tool_name: str, args: Any) -> list[tuple[str, str]]:
    """``[(raw_path, kind)]`` named by one tool call; empty for other tools."""
    kind = _TOOL_KIND.get(tool_name)
    if kind is None or not isinstance(args, dict):
        return []
    path = args.get("path")
    if isinstance(path, str) and path.strip():
        return [(path.strip(), kind)]
    if tool_name == "patch" and isinstance(args.get("patch"), str):
        found: list[tuple[str, str]] = []
        for m in _V4A_HEADER.finditer(args["patch"]):
            for p in (m.group(1), m.group(2), m.group(3)):
                if p:
                    found.append((p.strip(), WRITE))
        return found
    return []


def _dedupe(touches: Iterable[Touch]) -> list[Touch]:
    seen: set[tuple[str, str]] = set()
    out: list[Touch] = []
    for t in touches:
        if (t.path, t.kind) not in seen:
            seen.add((t.path, t.kind))
            out.append(t)
    return out


class ActivityLog:
    """Append-only, size-bounded touch log per project folder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def record(self, map_dir: Path, touches: list[Touch]) -> list[Touch]:
        touches = _dedupe(touches)
        if not touches:
            return []
        log = map_dir / "activity.jsonl"
        with self._lock:
            map_dir.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                for t in touches:
                    fh.write(json.dumps(asdict(t)) + "\n")
            self._trim(log)
        return touches

    @staticmethod
    def _trim(log: Path) -> None:
        lines = log.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_LOG_LINES:
            keep = lines[-(MAX_LOG_LINES // 2):]
            log.write_text("\n".join(keep) + "\n", encoding="utf-8")

    @staticmethod
    def read(map_dir: Path, limit: int = 200) -> list[dict[str, Any]]:
        try:
            lines = (map_dir / "activity.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-max(1, limit):]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def make_touches(raw: list[tuple[str, str]], *, tool: str, session_id: str,
                 resolve) -> list[Touch]:
    """Resolve raw paths to project-relative ones, dropping outside paths."""
    now = time.time()
    out: list[Touch] = []
    for path, kind in raw:
        rel = resolve(path)
        if rel:
            out.append(Touch(path=rel, kind=kind, tool=tool, session_id=session_id, ts=now))
    return out
