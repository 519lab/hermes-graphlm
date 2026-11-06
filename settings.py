"""Plugin settings — read once from ``plugins.entries.graphlm.settings``.

Every value has a safe default and is clamped, so a typo in config.yaml
degrades to a sane bound instead of an unbounded LLM spend or a hung scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

ENRICH_MODES = ("auto", "manual", "off")


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


@dataclass(frozen=True, slots=True)
class Settings:
    """What the plugin does automatically, and how much it may spend."""

    auto_map: bool = True          # build the free structural map on session start
    enrich: str = "auto"           # auto | manual | off — LLM summaries via ctx.llm
    max_files: int = 400           # files scanned per repo
    max_pass2_files: int = 60      # files the model reads during enrichment
    max_context: int = 60_000      # input-token budget for enrichment
    max_output_tokens: int = 32_000
    llm_timeout: int = 300         # seconds per enrichment pass
    context_chars: int = 3_000     # cap on the map summary injected into prompts

    @classmethod
    def from_config(cls, get: Callable[[str, Any], Any]) -> "Settings":
        d = cls()
        enrich = str(get("enrich", d.enrich) or d.enrich).strip().lower()
        return cls(
            auto_map=_bool(get("auto_map", d.auto_map), d.auto_map),
            enrich=enrich if enrich in ENRICH_MODES else d.enrich,
            max_files=_int(get("max_files", d.max_files), d.max_files, 20, 5_000),
            max_pass2_files=_int(get("max_pass2_files", d.max_pass2_files),
                                 d.max_pass2_files, 5, 200),
            max_context=_int(get("max_context", d.max_context), d.max_context,
                             8_000, 400_000),
            max_output_tokens=_int(get("max_output_tokens", d.max_output_tokens),
                                   d.max_output_tokens, 2_000, 128_000),
            llm_timeout=_int(get("llm_timeout", d.llm_timeout), d.llm_timeout, 30, 1_800),
            context_chars=_int(get("context_chars", d.context_chars),
                               d.context_chars, 500, 3_800),
        )
