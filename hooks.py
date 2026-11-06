"""Hermes hook callbacks and the build-event fan-out.

Every callback here is cheap and non-blocking: it reads small files, queues
work on the builder, or broadcasts an event. Builds themselves happen on the
builder's worker thread (Hermes abandons hooks that run longer than 30 s).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from .activity import WRITE, ActivityLog, make_touches, paths_from_tool
from .builder import BuildEvent, MapBuilder
from .context import ContextProvider
from .repo import find_project_root, relative_to_root, repo_key
from .settings import Settings
from .store import ENRICHED, MapStore

log = logging.getLogger(__name__)

PLUGIN_ID = "graphlm"
SUMMARIES_READY = ("Module and file summaries are now available — graphlm_lookup(path) "
                   "shows what a file does before you open it.")

Broadcast = Callable[[str, str, Optional[dict[str, Any]]], None]


class HookHandlers:
    """Glue between Hermes hooks, the builder, the context provider and the UI."""

    def __init__(self, *, settings: Settings, store: MapStore, builder_factory: Callable[
                 [Callable[[BuildEvent], None]], MapBuilder],
                 cwd: Callable[[], Optional[str]], surface: Callable[[], str],
                 broadcast: Broadcast) -> None:
        self._settings = settings
        self._store = store
        self._cwd = cwd
        self._surface = surface
        self._broadcast = broadcast
        self._activity = ActivityLog()
        self.builder = builder_factory(self.on_build)
        self.context = ContextProvider(store, max_chars=settings.context_chars,
                                       ensure=self.ensure_map)

    # ---- map lifecycle -------------------------------------------------------
    def ensure_map(self, root: Path) -> bool:
        """True once ``root`` has a map, building the structural one now if needed."""
        if self._store.load(root) is not None:
            return True
        if not self._settings.auto_map:
            return False
        return self.builder.build_now(root).error is None

    def maybe_enrich(self, root: Path, *, interactive: bool) -> None:
        """Queue LLM summaries once per project — only for sessions that will last.

        A one-shot ``hermes chat -q`` exits after one turn, killing the worker
        mid-request: the tokens are spent and the result is lost. So the classic
        CLI starts enrichment from its second turn; the gateway behind Desktop,
        the TUI and messaging platforms outlives any turn and starts at once.
        """
        if interactive and self.builder.needs_enrichment(root):
            self.builder.request_enrichment(root)

    def _long_lived(self) -> bool:
        return self._surface() not in ("", "cli")

    def on_build(self, event: BuildEvent) -> None:
        version = int(event.info.get("version", 0))
        if event.impact.notable:
            self.context.note(event.root, version, event.impact.message())
        if event.layer == ENRICHED and not event.error and event.info.get("status") == "ready":
            self.context.note(event.root, version, SUMMARIES_READY)
        self._send("map.updated", {
            "key": repo_key(event.root), "layer": event.layer, "info": event.info,
            "impact": event.impact.to_dict(), "error": event.error,
        })

    # ---- Hermes hooks (keyword args; tolerate future fields) -------------------
    def on_session_start(self, **_: Any) -> None:
        root = find_project_root(self._cwd())
        if root is not None and self.ensure_map(root):
            self.maybe_enrich(root, interactive=self._long_lived())

    def pre_llm_call(self, session_id: Optional[str] = None,
                     is_first_turn: Optional[bool] = None,
                     **_: Any) -> Optional[dict[str, str]]:
        cwd = self._cwd()
        text = self.context.for_turn(session_id, cwd)
        if is_first_turn is False:
            root = find_project_root(cwd)
            if root is not None:
                self.maybe_enrich(root, interactive=True)
        return {"context": text} if text else None

    def post_tool_call(self, tool_name: str = "", args: Any = None, status: str = "",
                       session_id: Optional[str] = None, **_: Any) -> None:
        if status != "ok":
            return
        raw = paths_from_tool(tool_name, args)
        if not raw:
            return
        cwd = self._cwd()
        root = find_project_root(cwd)
        if root is None:
            return
        touches = make_touches(raw, tool=tool_name, session_id=session_id or "",
                               resolve=lambda p: relative_to_root(root, p, cwd))
        recorded = self._activity.record(self._store.dir_for(root), touches)
        if not recorded:
            return
        self._send("activity", {
            "key": repo_key(root),
            "touches": [{"path": t.path, "kind": t.kind, "ts": t.ts} for t in recorded],
        })
        if any(t.kind == WRITE for t in recorded):
            self.builder.request(root)

    # ---- UI events -------------------------------------------------------------
    def _send(self, event: str, payload: dict[str, Any]) -> None:
        try:
            self._broadcast(PLUGIN_ID, event, payload)
        except Exception as exc:  # the UI being absent is never an agent error
            log.debug("graphlm broadcast %s failed: %s", event, exc)
