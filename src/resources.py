from __future__ import annotations

from pathlib import Path
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def resource_root() -> Path:
    if is_frozen():
        return Path(str(getattr(sys, "_MEIPASS"))).resolve()
    return Path(__file__).resolve().parents[1]


def resource_path(relative: str | Path) -> Path:
    """Resolve a bundled read-only resource, never mutable user data."""

    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("resource path must be a safe relative path")
    return resource_root() / relative_path
