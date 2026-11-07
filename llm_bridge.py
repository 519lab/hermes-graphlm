"""graphlm's LLM passes, run on the model Hermes is already configured with.

The plugin never holds an API key: ``generate_graph(llm=...)`` hands each pass
to this backend, which calls ``ctx.llm.complete_structured`` (Hermes attaches
credentials, routing and billing). Requests go to the plugin's own auxiliary
task, so an operator can route map enrichment to a cheaper model under
``auxiliary.graphlm_enrich`` without touching the agent's main model.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from graphlm import LLMReply

TASK_KEY = "graphlm_enrich"


def _usage(raw: Any) -> Optional[dict[str, object]]:
    """Hermes usage (input/output tokens) → OpenAI names graphlm records."""
    if raw is None:
        return None
    get = raw.get if isinstance(raw, dict) else lambda k: getattr(raw, k, None)
    prompt, completion = get("input_tokens"), get("output_tokens")
    if prompt is None and completion is None:
        return None
    usage: dict[str, object] = {"prompt_tokens": prompt, "completion_tokens": completion}
    cost = get("cost_usd")
    if cost is not None:
        usage["cost_usd"] = cost
    return usage


class HermesBackend:
    """An ``graphlm.LLMBackend`` over ``ctx.llm``."""

    def __init__(self, llm: Any, *, task: Optional[str] = TASK_KEY) -> None:
        self._llm = llm
        self._task = task

    def __call__(self, system_prompt: str, user_prompt: str, *,
                 json_schema: Optional[dict[str, object]], schema_name: Optional[str],
                 timeout: float, max_output_tokens: int) -> LLMReply:
        # ctx.llm requires non-empty instructions AND at least one input block
        # (agent/plugin_llm.py::_structured_spec); graphlm's prompt is the input.
        kwargs: dict[str, Any] = {
            "instructions": "Follow the task below and answer with a single JSON object.",
            "input": [{"type": "text", "text": user_prompt}],
            "system_prompt": system_prompt,
            "max_tokens": max_output_tokens,
            "timeout": timeout,
            "purpose": "graphlm map enrichment",
            "json_mode": json_schema is None,
        }
        if json_schema is not None:
            kwargs["json_schema"] = json_schema
            kwargs["schema_name"] = schema_name
        if self._task:
            kwargs["task"] = self._task
        result = self._llm.complete_structured(**kwargs)
        parsed = getattr(result, "parsed", None)
        if getattr(result, "content_type", None) == "json" and parsed is not None:
            text = json.dumps(parsed)
        else:
            text = getattr(result, "text", "") or ""
        return LLMReply(text=text, usage=_usage(getattr(result, "usage", None)))


def register_task(ctx: Any) -> Optional[str]:
    """Register the enrichment auxiliary task; ``None`` if this Hermes lacks it."""
    register = getattr(ctx, "register_auxiliary_task", None)
    if register is None:
        return None
    try:
        register(
            TASK_KEY,
            display_name="graphlm map enrichment",
            description="Summarises a codebase's modules and files for the graphlm map.",
        )
    except (ValueError, TypeError):
        return None
    return TASK_KEY
