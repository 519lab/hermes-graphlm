# graphlm — a live codebase map for Hermes

**Hermes sees your repository as a map, and you can watch it move through it.**

The first time a session opens in a project, graphlm builds a parser-verified import graph of the repo with no model calls, in well under a second. It hands the agent the shape of the codebase before its first turn:

- the most-imported files;
- entry points;
- import cycles;
- map tools that answer *"what imports this?"* and *"what breaks if I change this?"* from the graph instead of a dozen greps.

On top of that:

- **Summaries on your model.** Once per project, graphlm asks your configured Hermes model to summarise modules and files, so the map knows what each file does. There are no extra API keys.
- **Edit warnings.** When an edit introduces an import cycle, the agent is told on its next turn.
- **Live view.** The **Code Map** page in Hermes Desktop shows the whole graph. Files pulse as the agent reads and edits them, with a trail of its path.

Built on [graphLM](https://github.com/519lab/graphLM), an open-source codebase mapper (PyPI `graphlm`): Tree-sitter import edges for Python, JavaScript/TypeScript, Java, Rust, Go, C#, C/C++ and PHP.

## Install

```bash
hermes plugins install graphlm          # from the Hermes plugin catalog
# or
hermes plugins install https://github.com/519lab/hermes-graphlm
hermes plugins enable graphlm
```

Hermes installs the Python dependency (`graphlm[all]`) when you enable the plugin. For the live map, open **Hermes Desktop → Capabilities → Plugins**, switch on the Desktop half of **graphlm — Code Map**, and open **Code Map** in the sidebar.

Requires Hermes Agent ≥ 0.23.0.

## What the agent gets

| | |
|---|---|
| **Map in context** | A compact summary (files, parser-verified import edges, most-imported files, entry points, cycles, module descriptions) in the system prompt from the first turn. It is frozen per session, so prompt caching is unaffected. |
| `graphlm_overview` | The 30-second tour: counts, hubs, entry points, architecture notes, map freshness. |
| `graphlm_lookup(path)` | What a file does (when summaries exist), what it imports, what imports it, whether it is in a cycle. Accepts a full path, a suffix, or a unique name like `config`. |
| `graphlm_find(query)` | Locate files by keyword across paths, module names, summaries and symbols. |
| `graphlm_impact(path, transitive?, symbol?)` | Blast radius: every file that imports this one, directly or through other files, with distance. With `symbol`, also which of those files actually use it, with line numbers. That's the grep an agent would otherwise run once per importer. |
| `graphlm_cycles` | Import cycles with members and risk scores. |
| **Edit warnings** | After the agent writes files, the map is rebuilt in the background. A new or resolved import cycle is reported once, on the next turn. |

The map tools make no model calls and answer from the stored map in about 10 ms. Only `graphlm_impact` with `symbol` reads files: the importers the map already named, each up to 512 KB, at most 100.

## Commands

- `/graphlm status` — map state for this project.
- `/graphlm refresh` — rebuild the structural map now.
- `/graphlm enrich` — (re)generate summaries.
- `/graphlm path` — where `GRAPH.md` lives.
- `hermes graphlm map [PATH]` / `hermes graphlm status [PATH]` / `hermes graphlm list` — the same from the shell.

## Configuration

`plugins.entries.graphlm.settings` in `config.yaml`, or Desktop → Settings → Plugins:

| Key | Default | Meaning |
|---|---|---|
| `auto_map` | `true` | Build the free structural map automatically. |
| `enrich` | `auto` | `auto`: summarise once per project, in conversational sessions only (Desktop, TUI, messaging; the classic CLI from its 2nd turn). `manual`: only on `/graphlm enrich`. `off`: never. |
| `max_files` | `400` | Files scanned per project (20–5000). |
| `max_pass2_files` | `60` | Files the model reads during enrichment. |
| `max_context` / `max_output_tokens` | `60000` / `32000` | Token budgets for enrichment. |
| `llm_timeout` | `300` | Seconds per enrichment pass. |
| `context_chars` | `3000` | Size of the map summary in the prompt. |

Enrichment runs on the auxiliary task `graphlm_enrich`, so you can route it to a cheaper model:

```yaml
auxiliary:
  graphlm_enrich:
    provider: openrouter
    model: some/cheap-model
```

## How it works

- **Project detection.** The project is the nearest git root above the session's workspace, or the nearest folder with a project manifest. Your home directory and `/` are never mapped.
- **Structural map.** graphLM's Tree-sitter parsers produce import edges and import cycles locally. Python imports are tagged by when they run, so a function-local import counts toward blast radius but never toward a cycle.
- **Summaries.** graphLM's two-pass prompt runs through `ctx.llm.complete_structured`; graphLM validates the reply.
- **Storage.** Maps live in `<HERMES_HOME>/plugin-data/graphlm/maps/<project-hash>/`, never in your repository. Summaries survive structural rebuilds; see [DECISIONS.md](DECISIONS.md).

## Disclosures

What graphlm does on your machine, so you can decide before enabling it:

- **Reads your project's source files.**
  - **Which repo:** the git repository (or manifest folder) the session is working in, capped at `max_files`.
  - **What's skipped:** dependency and build folders, secrets files and anything in `.graphlmignore`.
  - **Redaction:** file contents are redacted for secret-like patterns before anything is sent to a model.
  - **Symbol lookups:** `graphlm_impact` with `symbol` also reads the importing files it names, locally, to find the symbol's lines.
- **Sends project content to your configured model. Opt-out:** `enrich: off` or `manual`.
  - **What is sent:** with `enrich: auto`, once per project: the directory tree, then up to `max_pass2_files` files (skeletonised and redacted) as two requests through Hermes's `ctx.llm`. That is your provider and billing; graphlm holds no credentials and calls no other service.
  - **Measured cost:** about 100k tokens for a 333-file repository on grok-4.7.
  - **Visibility:** each call is logged in `agent.log` (`plugin_llm.complete_structured plugin=graphlm`).
- **Writes** only under `<HERMES_HOME>/plugin-data/graphlm/`: maps, `GRAPH.md` and a bounded log of which files the agent read or edited. It never writes into your repository.
- **Runs `git rev-parse HEAD`** (no shell) in the project, to stamp which commit a map describes.
- **Runs one background worker thread** for map builds. It exits with Hermes.
- **No telemetry.** It makes no network calls other than the `ctx.llm` requests above.
- **Desktop page:** reads only the plugin's own read-only API (`/api/plugins/graphlm/…`) and Hermes's own tool events.

## Development

See [CLAUDE.md](CLAUDE.md). In short: `pytest tests`, and `hermes plugins validate . --install-deps` (what catalog CI runs).

## License

GPL-3.0-or-later, the same as graphLM.
