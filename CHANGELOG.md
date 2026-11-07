# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **A codebase map for every project the agent works in.**
  - **Automatic:** on the first visit, a parser-verified import graph (Tree-sitter, 8 languages) is built in a fraction of a second, with no model calls, and put in the system prompt so even the first turn knows the repo's shape.
  - **Kept fresh:** it is refreshed at every session start and after every edit the agent makes.
- **Five map tools:**
  - `graphlm_overview`: hubs, entry points, cycles, freshness;
  - `graphlm_lookup`: what a file does, imports, and is imported by;
  - `graphlm_find`: locate code by keyword;
  - `graphlm_impact`: blast radius, direct or transitive. With `symbol`, it also shows which importers actually use the symbol, with line numbers;
  - `graphlm_cycles`.
- **Module summaries on your own model.**
  - **Where it runs:** once per project, through Hermes's `ctx.llm`, so there are no extra API keys.
  - **When:** automatically only in conversational sessions; `/graphlm enrich` on demand; `enrich: off` disables it.
  - **Routing:** operators can send it to a cheap model via `auxiliary.graphlm_enrich`.
- **Cycle warnings.** When an edit introduces (or removes) an import cycle, the agent is told on its next turn and Desktop shows a toast. Only parser-proven, import-time imports count.
- **Code Map page in Hermes Desktop:**
  - the repo as a live graph;
  - files pulse as the agent reads (accent) or edits (warning) them, with a trail of its path;
  - cycles ringed;
  - a file panel with importers and imports.
- **Commands:** `/graphlm [status|refresh|enrich|path]`, and `hermes graphlm [map|status|list]`.

### Infrastructure
- **Test client on `httpx2`:** the dev/CI test deps swap `httpx` for `httpx2` and require `starlette>=1.2` (the first release whose `TestClient` uses it). Starlette 1.10 deprecates driving `TestClient` with plain `httpx`; tests now run warning-free.
- **CI actions on current Node 24 releases:** `actions/checkout` v7, `actions/setup-node` v7, and `astral-sh/setup-uv` pinned to the immutable `v10.3.3`. setup-uv publishes no floating `@vN` tags past v7, so an exact pin is the only way onto current releases; the old v4/v6 actions targeted the deprecated Node 20 runtime.
