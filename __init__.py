"""graphlm — a live codebase map for Hermes.

Wiring only: build the components, register them with Hermes. Behaviour lives
in the modules this imports (store, builder, context, tools, hooks, commands).
"""

from __future__ import annotations

from typing import Any, Optional

from .builder import MapBuilder
from .commands import USAGE, Commands
from .hooks import PLUGIN_ID, HookHandlers
from .llm_bridge import HermesBackend, register_task
from .settings import Settings
from .store import MapStore
from .tools import SCHEMAS, TOOLSET, MapTools


def _broadcast(plugin_id: str, event: str, payload: Optional[dict[str, Any]]) -> None:
    from hermes_cli.plugin_events import broadcast_plugin_event

    broadcast_plugin_event(plugin_id, event, payload)


def register(ctx: Any) -> None:
    from plugins.plugin_storage import plugin_data_dir

    settings = Settings.from_config(ctx.get_config)
    store = MapStore(plugin_data_dir(PLUGIN_ID) / "maps")
    task = register_task(ctx)

    def cwd() -> Optional[str]:
        return ctx.session_cwd() or None

    def surface() -> str:
        get = getattr(ctx, "session_surface", None)
        return (get() or "") if get is not None else ""

    def backend() -> Optional[HermesBackend]:
        llm = getattr(ctx, "llm", None)
        return HermesBackend(llm, task=task) if llm is not None else None

    hooks = HookHandlers(
        settings=settings, store=store, cwd=cwd, surface=surface, broadcast=_broadcast,
        builder_factory=lambda on_event: MapBuilder(store, settings, backend=backend,
                                                    on_event=on_event),
    )

    ctx.register_hook("on_session_start", hooks.on_session_start)
    ctx.register_hook("pre_llm_call", hooks.pre_llm_call)
    ctx.register_hook("post_tool_call", hooks.post_tool_call)
    ctx.register_system_prompt_section("graphlm.map", hooks.context.section,
                                       max_chars=settings.context_chars)

    tools = MapTools(store, hooks.builder, cwd)
    for name, handler in tools.handlers().items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=SCHEMAS[name], handler=handler,
                          description=SCHEMAS[name]["description"], emoji="🗺️")

    commands = Commands(store, hooks.builder, cwd)
    ctx.register_command("graphlm", handler=commands.slash, description="Codebase map status "
                         "and refresh", args_hint="[status|refresh|enrich|path]")
    ctx.register_cli_command("graphlm", help="Codebase maps (graphlm)",
                             setup_fn=Commands.setup_cli, handler_fn=commands.run_cli,
                             description=USAGE)
    on_unload = getattr(ctx, "on_unload", None)
    if on_unload is not None:
        on_unload(hooks.builder.stop)
