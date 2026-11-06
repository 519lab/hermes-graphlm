"""Fake Hermes objects used across the plugin tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeResult:
    text: str = ""
    parsed: Any = None
    content_type: str = "text"
    usage: Any = None


class FakeLlm:
    """Stands in for ``ctx.llm``: pass 1 → requested files, pass 2 → a graph."""

    def __init__(self, *, fail: bool = False, modules: list[dict] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail
        self.modules = modules if modules is not None else [
            {"name": "util", "path": "app/util.py", "description": "Shared helpers."},
        ]

    def complete_structured(self, **kwargs: Any) -> FakeResult:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("provider rejected the request")
        usage = {"input_tokens": 120, "output_tokens": 30, "cost_usd": 0.001}
        if kwargs.get("json_schema") is None:
            return FakeResult(parsed={"requested_files": ["app/util.py"]},
                              content_type="json", usage=usage)
        graph = {
            "directory_tree": "", "import_edges": [], "modules": self.modules,
            "file_summaries": [{"path": "app/util.py", "summary": "helper()",
                                "symbols": []}],
            "data_flow": [], "database_schema": None, "test_organization": [],
            "architecture_notes": [{"note": "tiny"}], "quick_reference": [],
        }
        return FakeResult(text=json.dumps(graph), parsed=graph, content_type="json",
                          usage=usage)


@dataclass
class FakeCtx:
    """Records everything ``register(ctx)`` does."""

    cwd: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    llm: Any = field(default_factory=FakeLlm)
    hooks: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    cli: dict[str, dict[str, Any]] = field(default_factory=dict)
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    aux_tasks: list[str] = field(default_factory=list)
    unload: list[Any] = field(default_factory=list)

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def session_cwd(self) -> str:
        return self.cwd

    def register_hook(self, name: str, cb: Any) -> None:
        self.hooks[name] = cb

    def register_tool(self, **kw: Any) -> None:
        self.tools[kw["name"]] = kw

    def register_command(self, name: str, handler: Any, **kw: Any) -> None:
        self.commands[name] = {"handler": handler, **kw}

    def register_cli_command(self, name: str, **kw: Any) -> None:
        self.cli[name] = kw

    def register_system_prompt_section(self, sid: str, content: Any, **kw: Any) -> None:
        self.sections[sid] = {"content": content, **kw}

    def register_auxiliary_task(self, key: str, **kw: Any) -> None:
        self.aux_tasks.append(key)

    def on_unload(self, cb: Any) -> None:
        self.unload.append(cb)
