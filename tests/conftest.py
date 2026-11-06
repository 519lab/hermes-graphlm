"""Shared fixtures: the plugin loaded as a package, fake Hermes context, repos."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PKG = "graphlm_plugin"


def _load_plugin() -> types.ModuleType:
    """Import the repo root as package ``graphlm_plugin`` (its dir name has a hyphen)."""
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(
        PKG, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


@pytest.fixture
def mod():
    """Access plugin submodules: ``mod('store').MapStore``."""
    return lambda name: importlib.import_module(f"{PKG}.{name}")


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@example.com", "-c",
                    "user.name=t", "-c", "commit.gpgsign=false", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@example.com", "-c",
                    "user.name=t", "-c", "commit.gpgsign=false", "commit", "-q", "-m",
                    "init"], check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small git repo: app/ with an a↔b import cycle and a shared util."""
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='proj'\n")
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "util.py").write_text("def helper():\n    return 1\n")
    (root / "app" / "a.py").write_text("from app import b\nfrom app import util\n")
    (root / "app" / "b.py").write_text("from app import a\nfrom app import util\n")
    (root / "app" / "main.py").write_text("from app import util\n\nutil.helper()\n")
    _git_init(root)
    return root


@pytest.fixture
def acyclic_repo(tmp_path: Path) -> Path:
    root = tmp_path / "flat"
    (root / "pkg").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='flat'\n")
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "x.py").write_text("from pkg import y\n")
    (root / "pkg" / "y.py").write_text("VALUE = 1\n")
    _git_init(root)
    return root


@pytest.fixture
def hermes(tmp_path: Path, monkeypatch):
    """Fake the two public Hermes modules the plugin imports; collect broadcasts."""
    events: list[tuple[str, str, Any]] = []
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: tmp_path / "plugin-data" / name
    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.plugin_storage = storage
    pe = types.ModuleType("hermes_cli.plugin_events")
    pe.broadcast_plugin_event = lambda pid, ev, payload=None: events.append((pid, ev, payload))
    cli_pkg = types.ModuleType("hermes_cli")
    cli_pkg.plugin_events = pe
    for name, m in {"plugins": plugins_pkg, "plugins.plugin_storage": storage,
                    "hermes_cli": cli_pkg, "hermes_cli.plugin_events": pe}.items():
        monkeypatch.setitem(sys.modules, name, m)
    return types.SimpleNamespace(events=events, data=tmp_path / "plugin-data" / "graphlm")
