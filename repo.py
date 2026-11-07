"""Which project a session is working in — and which ones we refuse to map.

A git checkout is the unit: the nearest ancestor holding ``.git`` wins, so a
session opened in ``src/pkg`` maps the whole repository. Without git, the
nearest directory with a project manifest is used. The home directory and the
filesystem root are never mapped (nor anything above them), so a session
started in ``~`` does not scan a whole home folder.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

PROJECT_MARKERS = (
    "pyproject.toml", "setup.py", "package.json", "go.mod", "Cargo.toml",
    "pom.xml", "build.gradle", "build.gradle.kts", "composer.json",
    "CMakeLists.txt", "Gemfile", "mix.exs",
)


def _candidates(start: Path) -> list[Path]:
    """``start`` and its ancestors, stopping below ``$HOME`` and ``/``."""
    try:
        home = Path.home().resolve()
    except RuntimeError:  # no resolvable home (some service accounts)
        home = None
    out: list[Path] = []
    for d in (start, *start.parents):
        if d == home or d.parent == d:
            break
        out.append(d)
    return out


def find_project_root(cwd: str | Path | None) -> Path | None:
    """The project root for ``cwd``, or ``None`` when there is nothing to map."""
    if not cwd:
        return None
    try:
        start = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not start.is_dir():
        return None
    dirs = _candidates(start)
    for d in dirs:
        if (d / ".git").exists():
            return d
    for d in dirs:
        if any((d / m).exists() for m in PROJECT_MARKERS):
            return d
    return None


def repo_key(root: Path) -> str:
    """Stable, filesystem-safe id for a project root (its storage folder name)."""
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def relative_to_root(root: Path, path: str | Path, cwd: str | Path | None) -> str | None:
    """``path`` (absolute, or relative to ``cwd``) as a POSIX path inside ``root``.

    ``None`` when the path lies outside the project — the agent reading
    ``/etc/hosts`` is not activity on this map.
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd or root) / p
    try:
        rel = p.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    text = rel.as_posix()
    return None if text in ("", ".") else text
