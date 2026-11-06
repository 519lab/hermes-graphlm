"""What an edit did to the project's structure.

Compares two structural maps (before and after the agent wrote files) using
only parser-proven facts — import edges and the import cycles they form — so
a warning such as "your edit introduced a cycle" is never an LLM guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from graphlm.llm import CodebaseGraph
from graphlm.view import norm_path


def _edges(graph: CodebaseGraph) -> set[tuple[str, str]]:
    return {
        (norm_path(e.from_path), norm_path(e.to_path))
        for e in graph.deterministic_edges or []
    }


def _cycles(graph: CodebaseGraph) -> dict[frozenset[str], list[str]]:
    """Production cycles keyed by member set (order-independent identity)."""
    out: dict[frozenset[str], list[str]] = {}
    for c in graph.import_cycles:
        if c.test_only:
            continue
        nodes = [norm_path(n) for n in c.nodes]
        out[frozenset(nodes)] = sorted(nodes)
    return out


@dataclass(frozen=True, slots=True)
class Impact:
    """The structural delta between two maps of the same project."""

    new_cycles: list[list[str]] = field(default_factory=list)
    resolved_cycles: list[list[str]] = field(default_factory=list)
    added_edges: list[tuple[str, str]] = field(default_factory=list)
    removed_edges: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.new_cycles or self.resolved_cycles
                    or self.added_edges or self.removed_edges)

    @property
    def notable(self) -> bool:
        """Worth interrupting the agent for: a cycle appeared or went away."""
        return bool(self.new_cycles or self.resolved_cycles)

    def message(self, limit: int = 3) -> str:
        """One short paragraph for the agent; empty when nothing notable."""
        lines: list[str] = []
        for cyc in self.new_cycles[:limit]:
            lines.append("New import cycle introduced: " + " → ".join([*cyc, cyc[0]])
                         + ". Consider breaking it before continuing.")
        for cyc in self.resolved_cycles[:limit]:
            lines.append("Import cycle resolved: " + " → ".join([*cyc, cyc[0]]) + ".")
        return " ".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "new_cycles": self.new_cycles,
            "resolved_cycles": self.resolved_cycles,
            "added_edges": [list(e) for e in self.added_edges],
            "removed_edges": [list(e) for e in self.removed_edges],
        }


def compare(before: CodebaseGraph | None, after: CodebaseGraph) -> Impact:
    """Delta from ``before`` to ``after``; empty when there is no baseline."""
    if before is None:
        return Impact()
    old_c, new_c = _cycles(before), _cycles(after)
    old_e, new_e = _edges(before), _edges(after)
    return Impact(
        new_cycles=[new_c[k] for k in sorted(new_c.keys() - old_c.keys(), key=sorted)],
        resolved_cycles=[old_c[k] for k in sorted(old_c.keys() - new_c.keys(), key=sorted)],
        added_edges=sorted(new_e - old_e),
        removed_edges=sorted(old_e - new_e),
    )
