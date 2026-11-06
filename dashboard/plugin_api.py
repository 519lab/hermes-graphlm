"""HTTP API for the live map page — mounted at ``/api/plugins/graphlm/``.

Hermes imports this file on its own (not as part of the plugin package, and
possibly in another process), so it shares nothing in memory with the agent
side: it only reads the files ``store.py`` writes atomically under
``plugin-data/graphlm/maps/<key>/``. Read-only; no route changes anything.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from plugins.plugin_storage import plugin_data_dir

router = APIRouter()

_KEY = re.compile(r"^[0-9a-f]{16}$")
_MAX_ACTIVITY = 500


def _maps_dir() -> Path:
    return plugin_data_dir("graphlm") / "maps"


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _list_maps() -> list[dict[str, Any]]:
    base = _maps_dir()
    if not base.is_dir():
        return []
    infos = [_read_json(d / "info.json") for d in base.iterdir() if d.is_dir()]
    return sorted((i for i in infos if isinstance(i, dict)),
                  key=lambda i: -float(i.get("updated_at") or 0))


def _resolve_key(key: Optional[str]) -> str:
    """The requested project, or the most recently updated one."""
    if key:
        if not _KEY.match(key):
            raise HTTPException(status_code=400, detail="invalid map key")
        return key
    maps = _list_maps()
    if not maps:
        raise HTTPException(status_code=404, detail="no projects mapped yet")
    return str(maps[0]["key"])


def _graph(key: Optional[str]) -> dict[str, Any]:
    k = _resolve_key(key)
    d = _maps_dir() / k
    view, info = _read_json(d / "view.json"), _read_json(d / "info.json")
    if view is None or info is None:
        raise HTTPException(status_code=404, detail="map not built yet")
    return {"key": k, "info": info, "view": view}


def _activity(key: Optional[str], limit: int) -> dict[str, Any]:
    k = _resolve_key(key)
    lines: list[str]
    try:
        lines = (_maps_dir() / k / "activity.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    rows = []
    for line in lines[-max(1, min(limit, _MAX_ACTIVITY)):]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return {"key": k, "touches": rows}


@router.get("/maps")
async def maps() -> dict[str, Any]:
    return {"maps": await asyncio.to_thread(_list_maps)}


@router.get("/graph")
async def graph(key: Optional[str] = None) -> dict[str, Any]:
    return await asyncio.to_thread(_graph, key)


@router.get("/activity")
async def activity(key: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
    return await asyncio.to_thread(_activity, key, limit)
