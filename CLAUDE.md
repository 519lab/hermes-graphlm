# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository.

## What this is

`graphlm` is a **Hermes Agent plugin**. The repository root *is* the plugin package: `plugin.yaml` + `__init__.py`, with a `desktop/` half and a `dashboard/` backend. It wraps the `graphlm` PyPI library (repo `519lab/graphLM`) and gives a Hermes agent:
- an instant structural map of the repo it works in;
- LLM summaries generated on Hermes's own model;
- five map tools;
- cycle warnings after edits;
- a live Desktop page.

Design decisions: `DECISIONS.md` (ADR-001…004). User docs: `README.md`.

## Branch model

`develop` (default, integration) and `main` (released). Every PR targets `develop`. Promotion `develop` → `main` is the owner's call, never squashed. `.github/workflows/promote.yml` (the shared `ggrace519/.github` workflow, pinned to v1.1.0's SHA) opens and refreshes that promotion PR after `ci` passes on a develop push; an open promotion PR is not approval. Its `workflows: [ci]` must match `ci.yml`'s `name:` exactly — rename one, rename both. `.github/workflows/release.yml` (same repo, same pin) tags `vX.Y.Z` and publishes the GitHub release from the top dated `CHANGELOG.md` section on every push to `main`, and dry-runs on the promotion PR. So **every promotion is a release**: before promoting, roll `[Unreleased]` into `## [X.Y.Z] - YYYY-MM-DD` and bump the version in `pyproject.toml`, `plugin.yaml` and `dashboard/manifest.json` — a promotion without a new version fails the dry run. Don't tag by hand.

## Layout (one job per module, all < 600 lines)

```
__init__.py        wiring only: build components, register with Hermes
settings.py        plugin config (plugins.entries.graphlm.settings), clamped
repo.py            project-root detection (git root, else manifest; never $HOME or /)
store.py           two-layer on-disk maps, merged view, atomic writes   (ADR-002)
builder.py         single background worker, dedupe, contextvars        (ADR-001)
llm_bridge.py      graphlm LLMBackend over ctx.llm.complete_structured
context.py         system-prompt section + per-turn notes, once per version
impact.py          parser-verified structural delta (new/resolved cycles)
activity.py        post_tool_call → file touches (incl. V4A patch headers)
hooks.py           Hermes hook callbacks + build-event fan-out, enrichment policy (ADR-003)
tools.py           graphlm_overview/lookup/find/impact/cycles
references.py      impact(symbol=): which importers actually use a symbol (bounded reads)
commands.py        /graphlm slash command + `hermes graphlm` CLI
dashboard/plugin_api.py   read-only /maps /graph /activity (imported standalone!)
desktop/plugin.js  Code Map page (single ES module, no build step)           (ADR-004)
bench/ab.py        with/without A/B over real `hermes chat` sessions
```

## Commands

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python "graphlm[all]>=0.7,<0.8" pytest pytest-cov pyyaml fastapi "starlette>=1.2" httpx2   # or -e "../graphLM[all]" to test against a local graphLM checkout
.venv/bin/python -m pytest tests                      # config in tests/pytest.ini (see below)
.venv/bin/python -m pytest tests --cov=. --cov-config=pyproject.toml --cov-report=term-missing
node --check desktop/plugin.js
hermes plugins validate . --install-deps              # what catalog CI runs — see the gotcha below
```

**Tests must run as `pytest tests`.** The repo root has an `__init__.py` (it is the plugin), so pytest would otherwise import it as a top-level module and its relative imports fail. `tests/pytest.ini` roots collection at `tests/`. `tests/conftest.py` loads the package as `graphlm_plugin`, and the fake Hermes objects are in `tests/fakes.py`.

## Developing against a real Hermes

- **Link and enable:** `ln -s "$PWD" ~/.hermes/plugins/graphlm && hermes plugins enable graphlm`.
- **One-shot queries run in a long-lived local gateway** (`--idle-exit`, 10 min). Python changes need `hermes plugins enable graphlm` (it hot-reloads the gateway's plugins; the prompt section and tools apply from the next session) or that gateway to exit.
- **The Desktop half** hot-reloads on save (the linked copy in `~/.hermes/desktop-plugins/graphlm`). It ships disabled: toggle it in Capabilities → Plugins, on the row titled by the desktop half's `name`.
- **`hermes plugins validate --install-deps` publishes into the live Hermes environment.** An unreleased `graphlm` needs a local wheel and `UV_FIND_LINKS=<dir>`. Hermes re-resolves only when requirements change, so the same version is not reinstalled.
- **Real-flow check:** `hermes chat -q "…" --oneshot --format stream-json --in <repo>` emits `tool_use` events. `~/.hermes/logs/agent.log` shows `Session plugin prompt section: id=graphlm.map` and `plugin_llm.complete_structured plugin=graphlm`.

## Invariants

- Never `os.getcwd()` in hooks or tools; use `ctx.session_cwd()`. Everything shares the gateway process.
- Never build or enrich inside a hook, except the first-visit structural build in the prompt section (ADR-001).
- The plugin calls `generate_graph(include_evidence=False, include_importance=False, include_html=False)`. No third-party calls besides `ctx.llm`, and no CDN.
- `dashboard/plugin_api.py` shares nothing in memory with the agent half; it only reads store files.
- Only public Hermes modules are imported (`plugins.plugin_storage`, `hermes_cli.plugin_events`). No core overrides.
