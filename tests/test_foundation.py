"""settings, repo, store, impact, activity, llm_bridge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from graphlm import generate_graph

from fakes import FakeLlm, FakeResult


# ---- settings ---------------------------------------------------------------
def test_settings_defaults_and_clamping(mod):
    S = mod("settings").Settings
    d = S.from_config(lambda k, default=None: default)
    assert d == S()
    cfg = {"auto_map": "no", "enrich": "BOGUS", "max_files": "999999",
           "max_context": "x", "context_chars": 10}
    s = S.from_config(lambda k, default=None: cfg.get(k, default))
    assert s.auto_map is False
    assert s.enrich == "auto"            # unknown mode → default
    assert s.max_files == 5_000          # clamped high
    assert s.max_context == S().max_context  # unparsable → default
    assert s.context_chars == 500        # clamped low
    assert S.from_config(lambda k, default=None: {"enrich": "Off"}.get(k, default)).enrich == "off"
    assert S.from_config(lambda k, default=None: {"auto_map": 0}.get(k, default)).auto_map is True


# ---- repo ---------------------------------------------------------------------
def test_find_project_root_prefers_git_root(mod, repo):
    find = mod("repo").find_project_root
    assert find(repo / "app") == repo
    assert find(str(repo)) == repo


def test_find_project_root_marker_fallback_and_refusals(mod, tmp_path, monkeypatch):
    r = mod("repo")
    proj = tmp_path / "nogit" / "src"
    proj.mkdir(parents=True)
    (tmp_path / "nogit" / "package.json").write_text("{}")
    assert r.find_project_root(proj) == tmp_path / "nogit"
    assert r.find_project_root(None) is None
    assert r.find_project_root(tmp_path / "missing") is None
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert r.find_project_root(bare) is None  # stops at $HOME, never maps it


def test_relative_to_root(mod, repo):
    rel = mod("repo").relative_to_root
    assert rel(repo, "app/a.py", repo) == "app/a.py"
    assert rel(repo, str(repo / "app" / "b.py"), "/elsewhere") == "app/b.py"
    assert rel(repo, "a.py", repo / "app") == "app/a.py"
    assert rel(repo, "/etc/hosts", repo) is None
    assert rel(repo, ".", repo) is None
    assert mod("repo").repo_key(repo) == mod("repo").repo_key(repo)


# ---- store --------------------------------------------------------------------
def test_store_layers_merge_and_versions(mod, repo, tmp_path):
    st = mod("store")
    store = st.MapStore(tmp_path / "maps")
    structure = generate_graph(repo, structure_only=True).graph
    served, info = store.save(repo, st.STRUCTURE, structure)
    d = store.dir_for(repo)
    assert {p.name for p in d.iterdir()} >= {"structure.json", "map.json", "view.json",
                                              "info.json", "GRAPH.md"}
    assert info["version"] == 1 and info["status"] == "ready"
    assert info["files"] == st.count_files(structure.directory_tree) == 6
    assert info["cycles"] == 1 and not info["enriched_at"]
    view = json.loads((d / "view.json").read_text())
    assert view["cycles"] == 1 and view["nodes"]

    enriched = structure.model_copy(deep=True)
    enriched.modules = []
    enriched.file_summaries = []
    from graphlm.models import ModuleDescription
    enriched.modules.append(ModuleDescription(name="u", path="app/util.py", description="d"))
    _, info2 = store.save(repo, st.ENRICHED, enriched)
    assert info2["version"] == 2 and info2["enriched_at"] and info2["modules"] == 1

    # A later structural rebuild keeps the paid-for summaries.
    _, info3 = store.save(repo, st.STRUCTURE, structure)
    assert info3["version"] == 3 and info3["enriched_at"] == info2["enriched_at"]
    assert store.load(repo).modules[0].path == "app/util.py"
    assert [m["root"] for m in store.list_maps()] == [str(repo)]
    with pytest.raises(ValueError):
        store.save(repo, "bogus", structure)


def test_store_status_and_errors(mod, repo, tmp_path):
    st = mod("store")
    store = st.MapStore(tmp_path / "maps")
    assert store.info(repo) is None and store.load(repo) is None
    assert store.list_maps() == []
    assert store.set_status(repo, "enriching")["status"] == "enriching"
    info = store.record_error(repo, st.ENRICHED, "boom" * 300)
    assert info["status"] == "error" and len(info["error"]["message"]) == 500
    (store.dir_for(repo) / "info.json").write_text("[1, 2]")
    assert store.info(repo) is None  # non-dict info is ignored, not trusted


# ---- impact -------------------------------------------------------------------
def test_impact_detects_new_and_resolved_cycles(mod, repo):
    imp = mod("impact")
    before = generate_graph(repo, structure_only=True).graph
    (repo / "app" / "b.py").write_text("from app import util\n")
    after = generate_graph(repo, structure_only=True).graph
    resolved = imp.compare(before, after)
    assert resolved.resolved_cycles == [["app/a.py", "app/b.py"]]
    assert resolved.removed_edges and resolved.notable and resolved.changed
    assert "resolved" in resolved.message()
    introduced = imp.compare(after, before)
    assert introduced.new_cycles == [["app/a.py", "app/b.py"]]
    assert "New import cycle introduced: app/a.py → app/b.py → app/a.py" in introduced.message()
    assert introduced.to_dict()["new_cycles"] == [["app/a.py", "app/b.py"]]
    none = imp.compare(None, after)
    assert not none.changed and none.message() == ""
    same = imp.compare(after, after)
    assert not same.notable


def test_impact_ignores_test_only_cycles(mod):
    from graphlm.llm import CodebaseGraph
    from graphlm.models import Cycle
    imp = mod("impact")
    base = CodebaseGraph(directory_tree="")
    tests_cycle = CodebaseGraph(directory_tree="", import_cycles=[
        Cycle(nodes=["tests/a.py", "tests/b.py"], edges=[], length=2, risk_score=1.0,
              test_only=True)])
    assert not imp.compare(base, tests_cycle).notable


# ---- activity -----------------------------------------------------------------
def test_paths_from_tool(mod):
    act = mod("activity")
    assert act.paths_from_tool("read_file", {"path": " app/a.py "}) == [("app/a.py", "read")]
    assert act.paths_from_tool("write_file", {"path": "x.py"}) == [("x.py", "write")]
    v4a = ("*** Begin Patch\n*** Update File: app/a.py\n@@\n-x\n+y\n"
           "*** Add File: app/new.py\n+z\n*** Delete File: old.py\n"
           "*** Move File: src/m.py -> src/n.py\n*** End Patch")
    assert act.paths_from_tool("patch", {"mode": "patch", "patch": v4a}) == [
        ("app/a.py", "write"), ("app/new.py", "write"), ("old.py", "write"),
        ("src/m.py", "write"), ("src/n.py", "write")]
    assert act.paths_from_tool("patch", {"path": "p.py", "old_string": "a"}) == [("p.py", "write")]
    assert act.paths_from_tool("terminal", {"command": "cat a.py"}) == []
    assert act.paths_from_tool("read_file", "not-a-dict") == []
    assert act.paths_from_tool("read_file", {"path": "  "}) == []


def test_activity_log_record_trim_read(mod, repo, tmp_path, monkeypatch):
    act = mod("activity")
    monkeypatch.setattr(act, "MAX_LOG_LINES", 10)
    log = act.ActivityLog()
    d = tmp_path / "m"
    touches = act.make_touches([("app/a.py", "read"), ("app/a.py", "read"),
                                ("/etc/passwd", "read")], tool="read_file",
                               session_id="s1",
                               resolve=lambda p: mod("repo").relative_to_root(repo, p, repo))
    assert [t.path for t in touches] == ["app/a.py", "app/a.py"]
    assert len(log.record(d, touches)) == 1  # deduped
    assert log.record(d, []) == []
    for i in range(20):
        log.record(d, [act.Touch(path=f"f{i}.py", kind="read", tool="t", session_id="",
                                 ts=float(i))])
    rows = act.ActivityLog.read(d, limit=100)
    assert len(rows) <= 10 and rows[-1]["path"] == "f19.py"
    (d / "activity.jsonl").write_text('{"path": "ok"}\nnot json\n')
    assert act.ActivityLog.read(d) == [{"path": "ok"}]
    assert act.ActivityLog.read(tmp_path / "none") == []


# ---- llm bridge ---------------------------------------------------------------
def test_backend_sends_valid_structured_request(mod):
    br = mod("llm_bridge")
    llm = FakeLlm()
    backend = br.HermesBackend(llm, task="graphlm_enrich")
    schema = {"type": "object"}
    reply = backend("SYS", "USER PROMPT", json_schema=schema, schema_name="CodebaseGraph",
                    timeout=12, max_output_tokens=999)
    call = llm.calls[0]
    assert call["instructions"].strip()
    assert call["input"] == [{"type": "text", "text": "USER PROMPT"}]
    assert call["system_prompt"] == "SYS" and call["json_schema"] is schema
    assert call["schema_name"] == "CodebaseGraph" and call["task"] == "graphlm_enrich"
    assert (call["timeout"], call["max_tokens"], call["json_mode"]) == (12, 999, False)
    assert json.loads(reply.text)["modules"][0]["path"] == "app/util.py"
    assert reply.usage == {"prompt_tokens": 120, "completion_tokens": 30, "cost_usd": 0.001}

    br.HermesBackend(llm, task=None)("s", "u", json_schema=None, schema_name=None,
                                     timeout=1, max_output_tokens=1)
    assert "task" not in llm.calls[-1] and llm.calls[-1]["json_mode"] is True


def test_backend_text_fallback_and_usage_shapes(mod):
    br = mod("llm_bridge")

    class TextLlm:
        def complete_structured(self, **kw):
            return FakeResult(text='{"a": 1}', content_type="text",
                              usage=type("U", (), {"input_tokens": 5, "output_tokens": 1,
                                                   "cost_usd": None})())

    reply = br.HermesBackend(TextLlm())("s", "u", json_schema=None, schema_name=None,
                                        timeout=1, max_output_tokens=1)
    assert reply.text == '{"a": 1}'
    assert reply.usage == {"prompt_tokens": 5, "completion_tokens": 1}
    assert br._usage(None) is None and br._usage({}) is None


def test_register_task(mod):
    br = mod("llm_bridge")
    assert br.register_task(object()) is None

    class Ok:
        keys: list = []
        def register_auxiliary_task(self, key, **kw):
            self.keys.append(key)

    class Bad:
        def register_auxiliary_task(self, key, **kw):
            raise ValueError("taken")

    assert br.register_task(Ok()) == br.TASK_KEY
    assert br.register_task(Bad()) is None
