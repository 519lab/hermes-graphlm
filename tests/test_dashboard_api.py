"""dashboard/plugin_api.py, loaded standalone the way Hermes mounts it."""

from __future__ import annotations

import importlib.util
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import PLUGIN_DIR


@pytest.fixture
def client(hermes, repo, mod):
    spec = importlib.util.spec_from_file_location(
        "graphlm_dashboard_api", PLUGIN_DIR / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/graphlm")
    yield TestClient(app)
    sys.modules.pop("graphlm_dashboard_api", None)


def _build(mod, hermes, root):
    st, b, s = mod("store"), mod("builder"), mod("settings")
    store = st.MapStore(hermes.data / "maps")
    b.MapBuilder(store, s.Settings(), backend=lambda: None,
                 on_event=lambda e: None).build_now(root)
    return store


def test_empty_state(client):
    assert client.get("/api/plugins/graphlm/maps").json() == {"maps": []}
    r = client.get("/api/plugins/graphlm/graph")
    assert r.status_code == 404 and "no projects" in r.json()["detail"]


def test_graph_and_activity(client, mod, hermes, repo):
    store = _build(mod, hermes, repo)
    key = store.info(repo)["key"]
    maps = client.get("/api/plugins/graphlm/maps").json()["maps"]
    assert [m["key"] for m in maps] == [key]
    g = client.get("/api/plugins/graphlm/graph").json()
    assert g["key"] == key and g["view"]["cycles"] == 1 and g["info"]["name"] == "proj"
    assert client.get(f"/api/plugins/graphlm/graph?key={key}").json()["key"] == key
    assert client.get("/api/plugins/graphlm/activity").json() == {"key": key, "touches": []}
    act = mod("activity")
    act.ActivityLog().record(store.dir_for(repo), [act.Touch(
        path="app/a.py", kind="read", tool="read_file", session_id="s", ts=1.0)])
    (store.dir_for(repo) / "activity.jsonl").open("a").write("garbage\n")
    rows = client.get(f"/api/plugins/graphlm/activity?key={key}&limit=5").json()["touches"]
    assert [r["path"] for r in rows] == ["app/a.py"]


def test_key_validation_and_missing_map(client, mod, hermes, repo):
    _build(mod, hermes, repo)
    assert client.get("/api/plugins/graphlm/graph?key=../../etc").status_code == 400
    assert client.get("/api/plugins/graphlm/graph?key=" + "0" * 16).status_code == 404
