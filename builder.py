"""Background map builds — never inside a hook, never two at once.

Hermes abandons hook callbacks after 30 s, and enrichment can take minutes, so
every build runs on one daemon worker thread. Each request carries a copy of
the caller's ``contextvars`` context: the session's profile and workspace live
in ContextVars, and ``ctx.llm`` must see the same ones the hook saw.

Requests for the same project and layer collapse while queued, so a burst of
edits costs one rebuild. A failed build is recorded in the project's
``info.json`` and reported through ``on_event``; it never raises into Hermes.
"""

from __future__ import annotations

import contextvars
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from graphlm import LLMBackend, generate_graph

from .impact import Impact, compare
from .settings import Settings
from .store import ENRICHED, STRUCTURE, MapStore

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BuildEvent:
    """What a finished (or failed) build tells the rest of the plugin."""

    root: Path
    layer: str
    info: dict[str, Any]
    impact: Impact = field(default_factory=Impact)
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class _Job:
    root: Path
    layer: str
    context: contextvars.Context


class MapBuilder:
    """Queue + single worker that turns project roots into stored maps."""

    def __init__(self, store: MapStore, settings: Settings, *,
                 backend: Callable[[], Optional[LLMBackend]],
                 on_event: Callable[[BuildEvent], None]) -> None:
        self._store = store
        self._settings = settings
        self._backend = backend
        self._on_event = on_event
        self._queue: "queue.Queue[_Job | None]" = queue.Queue()
        self._pending: set[tuple[Path, str]] = set()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    # ---- public API ------------------------------------------------------
    def request(self, root: Path, *, enrich: bool = False) -> bool:
        """Queue a structural build (and enrichment if asked); False if a dupe."""
        queued = self._enqueue(root, STRUCTURE)
        if enrich and self._settings.enrich != "off":
            queued = self._enqueue(root, ENRICHED) or queued
        return queued

    def request_enrichment(self, root: Path) -> bool:
        """Queue only the LLM layer (the structural map already exists)."""
        if self._settings.enrich == "off":
            return False
        return self._enqueue(root, ENRICHED)

    def needs_enrichment(self, root: Path) -> bool:
        """Auto mode, no summaries yet, and the last enrichment did not fail.

        A failed enrichment (provider rejects the schema, quota, timeout) is not
        retried automatically — otherwise every new session would pay for the
        same failure again. ``/graphlm enrich`` retries explicitly.
        """
        if self._settings.enrich != "auto":
            return False
        info = self._store.info(root) or {}
        if (info.get("error") or {}).get("layer") == ENRICHED:
            return False
        return not info.get("enriched_at")

    def build_now(self, root: Path, layer: str = STRUCTURE) -> BuildEvent:
        """Run one build synchronously (CLI, tools needing a map right now)."""
        return self._run(root, layer)

    def stop(self) -> None:
        self._stopped = True
        self._queue.put(None)

    # ---- worker ----------------------------------------------------------
    def _enqueue(self, root: Path, layer: str) -> bool:
        if self._stopped:
            return False
        key = (root, layer)
        with self._lock:
            if key in self._pending:
                return False
            self._pending.add(key)
            self._ensure_worker()
        self._queue.put(_Job(root, layer, contextvars.copy_context()))
        return True

    def _ensure_worker(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="graphlm-builder",
                                            daemon=True)
            self._thread.start()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until every queued build has finished; False on timeout."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                with self._lock:
                    self._pending.discard((job.root, job.layer))
                job.context.run(self._run, job.root, job.layer)
            finally:
                self._queue.task_done()

    # ---- one build -------------------------------------------------------
    def _run(self, root: Path, layer: str) -> BuildEvent:
        try:
            if layer == ENRICHED:
                event = self._enrich(root)
            else:
                event = self._structure(root)
        except Exception as exc:  # a broken build must never take Hermes down
            log.warning("graphlm %s build failed for %s: %s", layer, root, exc)
            info = self._store.record_error(root, layer, f"{type(exc).__name__}: {exc}")
            event = BuildEvent(root=root, layer=layer, info=info, error=str(exc))
        self._emit(event)
        return event

    def _structure(self, root: Path) -> BuildEvent:
        before = self._store.load_layer(root, STRUCTURE)
        result = generate_graph(
            root, structure_only=True, max_files=self._settings.max_files,
            include_html=False, include_diff=False,
        )
        _, info = self._store.save(root, STRUCTURE, result.graph)
        return BuildEvent(root=root, layer=STRUCTURE, info=info,
                          impact=compare(before, result.graph))

    def _enrich(self, root: Path) -> BuildEvent:
        backend = self._backend()
        if backend is None:
            raise RuntimeError("no model access for enrichment (ctx.llm unavailable)")
        self._emit(BuildEvent(root=root, layer=ENRICHED,
                              info=self._store.set_status(root, "enriching")))
        s = self._settings
        result = generate_graph(
            root, llm=backend, max_files=s.max_files, max_pass2_files=s.max_pass2_files,
            max_context=s.max_context, max_output_tokens=s.max_output_tokens,
            timeout=s.llm_timeout, include_evidence=False, include_importance=False,
            include_html=False, include_diff=False,
        )
        _, info = self._store.save(root, ENRICHED, result.graph)
        return BuildEvent(root=root, layer=ENRICHED, info=info)

    def _emit(self, event: BuildEvent) -> None:
        try:
            self._on_event(event)
        except Exception as exc:  # listeners must not kill the worker
            log.warning("graphlm build listener failed: %s", exc)
