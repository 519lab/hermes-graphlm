"""builder, context, tools, commands."""

from __future__ import annotations

import argparse
import contextvars
import json
from pathlib import Path

from fakes import FakeLlm


def _builder(mod, tmp_path, *, enrich="auto", llm=None, events=None):
    st, b, br, s = mod("store"), mod("builder"), mod("llm_bridge"), mod("settings")
    store = st.MapStore(tmp_path / "maps")
    settings = s.Settings(enrich=enrich)
    sink = events if events is not None else []
    backend = (lambda: br.HermesBackend(llm, task=None)) if llm is not None else (lambda: None)
    return store, b.MapBuilder(store, settings, backend=backend, on_event=sink.append), sink


# ---- builder ------------------------------------------------------------------
def test_builder_structure_then_enrichment(mod, repo, tmp_path):
    llm = FakeLlm()
    store, builder, events = _builder(mod, tmp_path, llm=llm)
    assert builder.needs_enrichment(repo)
    assert builder.request(repo, enrich=True)
    assert builder.wait_idle(30)
    layers = [(e.layer, e.info.get("status")) for e in events]
    assert ("structure", "ready") in layers
    assert ("enriched", "enriching") in layers and ("enriched", "ready") in layers
    info = store.info(repo)
    assert info["enriched_at"] and info["modules"] == 1 and info["summaries"] == 1
    assert info["enrich_usage"]["pass2"]["prompt_tokens"] == 120
    assert not builder.needs_enrichment(repo)
    assert len(llm.calls) == 2 and llm.calls[1]["json_schema"] is not None
    builder.stop()
    assert not builder.request(repo)


def test_builder_dedupes_queued_requests(mod, repo, tmp_path):
    store, builder, events = _builder(mod, tmp_path)
    builder._ensure_worker = lambda: None  # hold the queue: nothing runs yet
    assert builder.request(repo) is True
    assert builder.request(repo) is False
    assert builder._queue.qsize() == 1


def test_builder_enrichment_failure_is_recorded_and_not_auto_retried(mod, repo, tmp_path):
    store, builder, events = _builder(mod, tmp_path, llm=FakeLlm(fail=True))
    builder.request(repo, enrich=True)
    assert builder.wait_idle(30)
    failed = [e for e in events if e.error]
    assert failed and failed[0].layer == "enriched"
    assert store.info(repo)["error"]["layer"] == "enriched"
    assert not builder.needs_enrichment(repo)  # no silent paid retry loop


def test_builder_without_model_access_errors_cleanly(mod, repo, tmp_path):
    store, builder, events = _builder(mod, tmp_path, llm=None)
    event = builder.build_now(repo, "enriched")
    assert "no model access" in event.error


def test_builder_enrich_off_and_manual(mod, repo, tmp_path):
    store, builder, _ = _builder(mod, tmp_path, enrich="off")
    builder._ensure_worker = lambda: None
    builder.request(repo, enrich=True)
    assert builder._queue.qsize() == 1  # structure only
    assert builder.request_enrichment(repo) is False
    store2, manual, _ = _builder(mod, tmp_path / "m", enrich="manual")
    assert not manual.needs_enrichment(repo)


def test_builder_runs_jobs_in_the_requesters_context(mod, repo, tmp_path):
    var = contextvars.ContextVar("profile", default="none")
    seen: list[str] = []

    class Spy(FakeLlm):
        def complete_structured(self, **kw):
            seen.append(var.get())
            return super().complete_structured(**kw)

    store, builder, _ = _builder(mod, tmp_path, llm=Spy())
    token = var.set("work-profile")
    try:
        builder.request(repo, enrich=True)
    finally:
        var.reset(token)
    assert builder.wait_idle(30)
    assert seen and set(seen) == {"work-profile"}


def test_builder_survives_a_failing_listener(mod, repo, tmp_path):
    st, b, s = mod("store"), mod("builder"), mod("settings")
    store = st.MapStore(tmp_path / "maps")

    def boom(_):
        raise RuntimeError("listener exploded")

    builder = b.MapBuilder(store, s.Settings(), backend=lambda: None, on_event=boom)
    assert builder.build_now(repo).error is None


def test_wait_idle_times_out(mod, repo, tmp_path):
    store, builder, _ = _builder(mod, tmp_path)
    builder._ensure_worker = lambda: None
    builder.request(repo)
    assert builder.wait_idle(0.05) is False


# ---- context ------------------------------------------------------------------
def _provider(mod, store, ensure=None, chars=3000):
    return mod("context").ContextProvider(store, max_chars=chars,
                                          ensure=ensure or (lambda root: False))


def test_summary_content_and_bound(mod, repo, tmp_path):
    store, builder, _ = _builder(mod, tmp_path, llm=FakeLlm())
    builder.build_now(repo)
    builder.build_now(repo, "enriched")
    text = mod("context").summarize(store.load(repo), store.info(repo), 3000)
    assert "## Codebase map — proj (graphlm)" in text
    assert "1 import cycles" in text and "Import cycle: app/a.py → app/b.py → app/a.py" in text
    assert "Most imported: app/util.py (3)" in text
    assert "- app/util.py: Shared helpers." in text
    assert text.endswith(mod("context").TOOLS_HINT)
    short = mod("context").summarize(store.load(repo), store.info(repo), 500)
    assert len(short) <= 500 and short.endswith(mod("context").TOOLS_HINT)


def test_first_visit_builds_synchronously_so_the_first_prompt_has_the_map(mod, repo, tmp_path):
    store, builder, _ = _builder(mod, tmp_path)
    asked: list[Path] = []

    def ensure(root):
        asked.append(root)
        return builder.build_now(root).error is None

    ctx = _provider(mod, store, ensure)
    text = ctx.section({"cwd": str(repo / "app"), "session_id": "s1"})
    assert text.startswith("## Codebase map — proj") and asked == [repo]
    assert ctx.for_turn("s1", str(repo)) is None  # the section already carried it
    assert asked == [repo]  # no second build once the map exists


def test_no_map_and_no_build_means_no_context(mod, repo, tmp_path):
    store, builder, _ = _builder(mod, tmp_path)
    ctx = _provider(mod, store)  # ensure refuses (auto_map off)
    assert ctx.section({"cwd": str(repo), "session_id": "s1"}) == ""
    assert ctx.for_turn("s1", str(repo)) is None
    ctx_lying = _provider(mod, store, ensure=lambda root: True)  # claims success, no map
    assert ctx_lying.for_turn("s1", str(repo)) is None


def test_turns_deliver_each_version_once(mod, repo, tmp_path):
    store, builder, _ = _builder(mod, tmp_path)
    builder.build_now(repo)
    ctx = _provider(mod, store)
    # A session whose section lacked the map (moved repos) gets it once.
    first = ctx.for_turn("s1", str(repo))
    assert first and first.startswith("## Codebase map")
    assert ctx.for_turn("s1", str(repo)) is None
    # A session that got the map via the section hears nothing extra.
    assert ctx.section({"cwd": str(repo / "app"), "session_id": "s2"})
    assert ctx.for_turn("s2", str(repo)) is None
    # A structural change with a note is delivered once to each session.
    info = builder.build_now(repo).info
    ctx.note(repo, info["version"], "New import cycle introduced: x → y → x.")
    ctx.note(repo, info["version"], "")
    assert ctx.for_turn("s1", str(repo)) == "graphlm map update: New import cycle introduced: x → y → x."
    assert ctx.for_turn("s1", str(repo)) is None
    assert "New import cycle" in ctx.for_turn("s2", str(repo))
    # A version bump with nothing notable stays silent.
    builder.build_now(repo)
    assert ctx.for_turn("s1", str(repo)) is None
    # Outside a project there is nothing to say.
    assert ctx.for_turn("s1", str(tmp_path)) is None
    assert ctx.section(type("M", (), {"cwd": None, "session_id": None})()) == ""


def test_session_memory_is_bounded(mod, repo, tmp_path, monkeypatch):
    c = mod("context")
    monkeypatch.setattr(c, "MAX_SESSIONS", 3)
    store, builder, _ = _builder(mod, tmp_path)
    builder.build_now(repo)
    ctx = _provider(mod, store)
    for i in range(5):
        ctx.for_turn(f"s{i}", str(repo))
    assert list(ctx._seen) == ["s2", "s3", "s4"]
    ctx._remember(None, "k", 1)  # no session id: ignored
    assert len(ctx._seen) == 3


# ---- tools --------------------------------------------------------------------
def _tools(mod, tmp_path, cwd):
    store, builder, _ = _builder(mod, tmp_path)
    return mod("tools").MapTools(store, builder, lambda: cwd), store, builder


def test_tools_build_on_demand_and_answer(mod, repo, tmp_path):
    tools, store, _ = _tools(mod, tmp_path, str(repo))
    over = json.loads(tools.overview({}))
    assert over["result"]["counts"]["import_cycles"] == 1
    assert over["map"]["freshness"] == "fresh" and over["map"]["summaries"] == "not yet generated"
    look = json.loads(tools.lookup({"path": "util"}))["result"]
    assert look["path"] == "app/util.py"
    assert look["imported_by"] == 3 and look["imports"] == 0  # distinct-file counts
    assert look["in_cycles"] == []
    assert json.loads(tools.find({"query": "main", "limit": 500}))["result"]
    impact = json.loads(tools.impact({"path": "app/util.py", "transitive": True}))["result"]
    assert {d["path"] for d in impact["dependents"]} >= {"app/a.py", "app/b.py", "app/main.py"}
    cycles = json.loads(tools.cycles({}))["result"]
    assert cycles["cycles"] and set(cycles["cycles"][0]["nodes"]) == {"app/a.py", "app/b.py"}
    assert set(tools.handlers()) == set(mod("tools").SCHEMAS)


def test_tools_errors_are_json_not_exceptions(mod, repo, tmp_path):
    tools, store, builder = _tools(mod, tmp_path, str(tmp_path / "nowhere"))
    assert "not inside a project" in json.loads(tools.overview({}))["error"]
    assert json.loads(tools.lookup({}))["error"] == "path is required"
    assert json.loads(tools.impact({"path": " "}))["error"] == "path is required"
    assert json.loads(tools.find({"query": ""}))["error"] == "query is required"
    ok = json.loads(tools.overview({"project": str(repo)}))
    assert "result" in ok
    builder.build_now = lambda root, layer="structure": type(
        "E", (), {"error": "disk full", "info": {}})()
    fresh_tools = mod("tools").MapTools(mod("store").MapStore(tmp_path / "other"), builder,
                                        lambda: str(repo))
    assert "disk full" in json.loads(fresh_tools.overview({}))["error"]
    store.load = lambda root: (_ for _ in ()).throw(OSError("unreadable"))
    assert "OSError: unreadable" in json.loads(tools.overview({"project": str(repo)}))["error"]
    assert mod("tools")._clamp("x", 15, 1, 50) == 15


def test_tools_report_missing_map_after_failed_build(mod, repo, tmp_path):
    tools, store, builder = _tools(mod, tmp_path, str(repo))
    builder.build_now = lambda root, layer="structure": type("E", (), {"error": None})()
    assert "no map available" in json.loads(tools.overview({}))["error"]


# ---- commands -----------------------------------------------------------------
def test_slash_command(mod, repo, tmp_path):
    store, builder, _ = _builder(mod, tmp_path, llm=FakeLlm())
    cmd = mod("commands").Commands(store, builder, lambda: str(repo))
    assert cmd.slash("") .startswith("No map yet")
    refreshed = cmd.slash("refresh")
    assert "status: ready" in refreshed and "1 cycles" in refreshed
    assert "summaries: not generated" in cmd.slash("status")
    assert "background" in cmd.slash("enrich")
    assert builder.wait_idle(30)
    assert "summaries: 1 modules / 1 files" in cmd.slash("  STATUS  ")
    assert cmd.slash("path").endswith("GRAPH.md")
    assert cmd.slash("bogus") == mod("commands").USAGE
    outside = mod("commands").Commands(store, builder, lambda: str(tmp_path / "x"))
    assert "not inside a project" in outside.slash("status")
    builder.build_now = lambda root, layer="structure": type("E", (), {"error": "boom",
                                                                       "info": {}})()
    assert "rebuild failed — boom" in cmd.slash("refresh")
    store.record_error(repo, "enriched", "quota")
    assert "last error (enriched): quota" in mod("commands").describe(store.info(repo))
    store.set_status(repo, "enriching")
    assert "status: enriching ·" in mod("commands").describe(store.info(repo))
    stale = {**store.info(repo), "status_at": 0}
    assert "enrichment interrupted" in mod("commands").describe(stale)


def test_cli(mod, repo, tmp_path, capsys):
    store, builder, _ = _builder(mod, tmp_path)
    cmd = mod("commands").Commands(store, builder, lambda: None)
    parser = argparse.ArgumentParser()
    mod("commands").Commands.setup_cli(parser)
    assert cmd.run_cli(parser.parse_args(["list"])) == 0
    assert "No projects mapped yet." in capsys.readouterr().out
    assert cmd.run_cli(parser.parse_args(["map", str(repo)])) == 0
    assert "status: ready" in capsys.readouterr().out
    assert cmd.run_cli(parser.parse_args(["status", str(repo / "app")])) == 0
    assert "proj" in capsys.readouterr().out
    assert cmd.run_cli(parser.parse_args([])) == 0
    assert str(repo) in capsys.readouterr().out
    assert cmd.run_cli(parser.parse_args(["map", str(tmp_path / "missing")])) == 1
    builder.build_now = lambda root, layer="structure": type("E", (), {"error": "boom",
                                                                       "info": {}})()
    assert cmd.run_cli(parser.parse_args(["map", str(repo)])) == 1


# ---- symbol-aware impact ------------------------------------------------------
def test_impact_with_symbol_separates_users_from_mere_importers(mod, repo, tmp_path):
    (repo / "app" / "main.py").write_text("from app import util\n\nprint('done')\n")
    tools, store, _ = _tools(mod, tmp_path, str(repo))
    r = json.loads(tools.impact({"path": "app/util.py", "symbol": "helper"}))["result"]
    assert r["symbol"] == "helper"
    assert r["uses_symbol"] == [] and set(r["imports_without_using"]) == {
        "app/a.py", "app/b.py", "app/main.py"}
    (repo / "app" / "a.py").write_text("from app import b\nfrom app import util\nutil.helper()\n")
    r = json.loads(tools.impact({"path": "app/util.py", "symbol": "util.helper"}))["result"]
    assert r["uses_symbol"] == [{"path": "app/a.py", "lines": [3]}]
    assert "symbol must be an identifier" in json.loads(
        tools.impact({"path": "app/util.py", "symbol": "rm -rf"}))["error"]
    missing = json.loads(tools.impact({"path": "nope.py", "symbol": "x"}))["result"]
    assert "uses_symbol" not in missing


def test_find_references_bounds(mod, repo, tmp_path, monkeypatch):
    refs = mod("references")
    (repo / "big.py").write_text("helper\n" * 10)
    monkeypatch.setattr(refs, "MAX_BYTES", 60)  # a.py (37B) fits, big.py (70B) does not
    out = refs.find_references(repo, ["big.py", "../outside.py", "missing.py", "app/a.py"],
                               "helper")
    assert out == {"app/a.py": []}          # too big, outside root, missing → skipped
    monkeypatch.setattr(refs, "MAX_BYTES", 10**6)
    assert refs.find_references(repo, ["big.py"], "helper")["big.py"] == [1, 2, 3, 4, 5]
    assert refs.valid_symbol("Config.load") and not refs.valid_symbol("a b")
