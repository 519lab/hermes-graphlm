# Architecture Decision Records

Decisions for the `graphlm` Hermes plugin, newest first.

---

## ADR-004 — Desktop page: dependency-free force layout, three activity channels

**Status:** Accepted (2026-11-05)

**Context.** A listed Desktop plugin must be one ES module that imports only `@hermes/plugin-sdk`, `react` and `react/jsx-runtime`. The validator's desktop-surface check also refuses `new Function`. Full d3 trips that check (d3-dsv uses it), and a bundled third-party library would be more code for reviewers to read. Plugin events may not reach Desktop when the plugin runs in the out-of-process plugin host.

**Decision.**
- **Layout:** `desktop/plugin.js` uses a ~60-line deterministic force layout (pairwise repulsion, link springs, gravity) and draws with React SVG. Positions persist across map versions, so an edit nudges the picture instead of reshuffling it.
- **File activity reaches the page three ways:**
  - the native `tool.start` event, whose payload carries the tool's args, so a file pulses as the agent *starts* reading it;
  - the plugin's `activity` broadcast;
  - a 2.5 s poll of `/activity`, which works under any isolation mode.
- Pulses are de-duplicated per file per second.

**Consequences.** No build step, and the whole page is reviewable source. Layout is O(n²) per step, which is fine at the 400-file default and noticeably slower past ~1,500 nodes.

---

## ADR-003 — Enrichment (the only paid step) runs once per project, only in conversational sessions

**Status:** Accepted (2026-11-05)

**Context.** LLM summaries cost real tokens: ~100k on grok-4.7 for a 333-file repo, measured. A one-shot `hermes chat -q` from a script or cron started one as a side effect. A failed enrichment (schema rejected, quota) would be retried by every new session.

**Decision.**
- **`enrich: auto` (default):**
  - starts at session start on conversational surfaces (Desktop, TUI, messaging platforms);
  - on the classic CLI, starts from the second user turn;
  - runs once per project.
- **`manual`:** only on `/graphlm enrich`. **`off`:** never.
- A recorded enrichment error stops automatic retries until `/graphlm enrich`.
- Calls go through `ctx.llm` on the plugin's own auxiliary task (`graphlm_enrich`), so operators can route them to a cheap model under `auxiliary.graphlm_enrich`.

**Consequences.** One-shot scripts never pay for summaries. The structural map, which is free, is always available, and tools work without enrichment.

---

## ADR-002 — Two-layer map store, served merged

**Status:** Accepted (2026-11-05)

**Context.** The structural map is free and should track every edit. The LLM layer is expensive and should be kept.

**Decision.**
- **Layers:** each project folder under `plugin-data/graphlm/maps/<sha256(root)[:16]>/` keeps `structure.json` and `enriched.json`.
- **Served map:** `map.json` (with `GRAPH.md` and `view.json`) is the enrichment with its structural fields — tree, AST edges, cycles, meta — replaced by the newest structure.
- **Writes** are atomic replaces, because `dashboard/plugin_api.py` reads them from another import context or process.
- **Versioning:** `info.json` carries a version counter. The context provider tells each session about a given version at most once.

**Consequences.** Edit-triggered rebuilds never discard summaries. Summaries can describe a file that has since changed; `info.enriched_commit` records which commit they describe.

---

## ADR-001 — Map on first visit synchronously; everything else in the background

**Status:** Accepted (2026-11-05)

**Context.** Hermes abandons hook callbacks after 30 s. A first live run showed that a background-only build loses the race with the first prompt: the system-prompt section is assembled milliseconds after the session starts, and a one-shot run has no second turn.

**Decision.**
- **First visit:** when no map exists, the system-prompt section builds the structural map synchronously (local Tree-sitter parsing, capped by `max_files`, 0.1–0.3 s on real repos) and the first prompt carries it.
- **Background work:** every other build runs on one daemon worker thread, inside a copy of the requesting hook's `contextvars` context, so `ctx.llm` and session lookups see the right profile. That covers edit rebuilds, the session-start refresh of an existing map, and enrichment.
- **Deduplication:** identical queued requests collapse.

**Consequences.** Deterministic first-turn context; a very large repo can add up to a few seconds to its first session start (bounded by `max_files`).
