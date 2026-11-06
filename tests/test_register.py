"""register(ctx) wiring and the hook flow end to end, with a fake Hermes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from conftest import plugin
from fakes import FakeCtx, FakeLlm


def _registered(hermes, cwd, surface="cli", **config):
    ctx = FakeCtx(cwd=str(cwd), surface=surface, config=config, llm=FakeLlm())
    plugin.register(ctx)
    return ctx


def test_register_declares_exactly_the_manifest_surface(hermes, repo):
    ctx = _registered(hermes, repo)
    manifest = yaml.safe_load(Path(plugin.__file__).with_name("plugin.yaml").read_text())
    assert sorted(ctx.tools) == sorted(manifest["provides_tools"])
    assert sorted(ctx.hooks) == sorted(manifest["provides_hooks"])
    assert all(t["toolset"] == "graphlm" for t in ctx.tools.values())
    assert set(ctx.commands) == {"graphlm"} and set(ctx.cli) == {"graphlm"}
    assert ctx.sections["graphlm.map"]["max_chars"] == 3000
    assert ctx.aux_tasks == ["graphlm_enrich"] and len(ctx.unload) == 1


def test_session_flow_maps_injects_and_tracks_edits(hermes, repo):
    ctx = _registered(hermes, repo)
    hooks = ctx.hooks
    builder = ctx.unload[0].__self__
    # Session start in the CLI: the frozen prompt section carries the map
    # (built synchronously on this first visit); no paid enrichment yet.
    section = ctx.sections["graphlm.map"]["content"]({"cwd": str(repo), "session_id": "s1"})
    assert section.startswith("## Codebase map — proj")
    hooks["on_session_start"](session_id="s1", model="m", platform="cli")
    assert hooks["pre_llm_call"](session_id="s1", user_message="hi", is_first_turn=True) is None
    assert builder.wait_idle(30) and ctx.llm.calls == []

    # Reads are recorded and broadcast, outside paths ignored.
    hooks["post_tool_call"](tool_name="read_file", args={"path": "app/a.py"}, status="ok",
                            session_id="s1", result="{}")
    hooks["post_tool_call"](tool_name="read_file", args={"path": "/etc/hosts"}, status="ok")
    hooks["post_tool_call"](tool_name="read_file", args={"path": "app/b.py"}, status="error")
    hooks["post_tool_call"](tool_name="terminal", args={"command": "ls"}, status="ok")
    activity = [p for _, e, p in hermes.events if e == "activity"]
    assert [t["path"] for a in activity for t in a["touches"]] == ["app/a.py"]

    # The agent breaks the a↔b cycle with a write → rebuild → one-time note.
    (repo / "app" / "b.py").write_text("from app import util\n")
    hooks["post_tool_call"](tool_name="write_file", args={"path": "app/b.py"}, status="ok",
                            session_id="s1")
    assert builder.wait_idle(30)
    note = hooks["pre_llm_call"](session_id="s1")
    assert note["context"] == ("graphlm map update: Import cycle resolved: "
                               "app/a.py → app/b.py → app/a.py.")
    updates = [p for _, e, p in hermes.events if e == "map.updated"]
    assert updates[-1]["impact"]["resolved_cycles"] == [["app/a.py", "app/b.py"]]

    # ...and re-introducing it is flagged as new.
    (repo / "app" / "b.py").write_text("from app import a\n")
    hooks["post_tool_call"](tool_name="patch", args={
        "mode": "patch", "patch": "*** Begin Patch\n*** Update File: app/b.py\n*** End Patch"},
        status="ok", session_id="s1")
    assert builder.wait_idle(30)
    assert "New import cycle introduced" in hooks["pre_llm_call"](session_id="s1")["context"]

    # A second user turn proves the session is interactive → summaries start.
    hooks["pre_llm_call"](session_id="s1", is_first_turn=False)
    assert builder.wait_idle(30) and len(ctx.llm.calls) == 2
    info = json.loads((hermes.data / "maps").joinpath(
        next(p.name for p in (hermes.data / "maps").iterdir()), "info.json").read_text())
    assert info["enriched_at"] and info["modules"] == 1
    assert "summaries are now available" in hooks["pre_llm_call"](session_id="s1")["context"]


def test_one_shot_cli_never_pays_for_enrichment(hermes, repo):
    ctx = _registered(hermes, repo)
    ctx.hooks["on_session_start"](session_id="once")
    assert ctx.hooks["pre_llm_call"](session_id="once", is_first_turn=True)["context"]
    assert ctx.unload[0].__self__.wait_idle(30)
    assert ctx.llm.calls == []


def test_long_lived_surfaces_enrich_at_session_start(hermes, repo):
    ctx = _registered(hermes, repo, surface="desktop")
    ctx.hooks["on_session_start"](session_id="d1")
    assert ctx.unload[0].__self__.wait_idle(30)
    assert len(ctx.llm.calls) == 2


def test_auto_map_off_builds_nothing_until_asked(hermes, repo):
    ctx = _registered(hermes, repo, auto_map=False, enrich="manual")
    ctx.hooks["on_session_start"](session_id="s")
    assert ctx.hooks["pre_llm_call"](session_id="s") is None
    assert not (hermes.data / "maps").exists()
    out = ctx.commands["graphlm"]["handler"]("refresh")
    assert "status: ready" in out
    tool = ctx.tools["graphlm_overview"]["handler"]
    assert json.loads(tool({}))["result"]["counts"]["import_cycles"] == 1


def test_hooks_tolerate_no_project_and_broadcast_failures(hermes, tmp_path, repo):
    ctx = _registered(hermes, tmp_path / "not-a-project")
    ctx.hooks["on_session_start"]()
    assert ctx.hooks["pre_llm_call"](session_id="x") is None
    ctx.hooks["post_tool_call"](tool_name="read_file", args={"path": "a.py"}, status="ok")

    sys.modules["hermes_cli.plugin_events"].broadcast_plugin_event = (
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no desktop")))
    ctx2 = _registered(hermes, repo, enrich="off")
    ctx2.hooks["post_tool_call"](tool_name="read_file", args={"path": "app/a.py"},
                                 status="ok")  # must not raise


def test_register_without_llm(hermes, repo):
    ctx = FakeCtx(cwd=str(repo), llm=None)
    plugin.register(ctx)
    builder = ctx.unload[0].__self__
    assert "no model access" in builder.build_now(repo, "enriched").error
