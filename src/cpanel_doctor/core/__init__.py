"""Core building blocks for cPanel Doctor patches."""
from __future__ import annotations

import os

from .patch import Component, ComponentStatus, Patch, PatchState
from .registry import load_patches, patches_by_id
from .runner import Action, Runner, RunnerError

__all__ = [
    "Action",
    "Component",
    "ComponentStatus",
    "Patch",
    "PatchState",
    "Runner",
    "RunnerError",
    "asset_text",
    "load_patches",
    "patches_by_id",
]


def asset_text(name: str) -> str:
    """Read a packaged asset (works for installed wheels and source checkouts)."""
    try:  # Python 3.9+
        from importlib.resources import files

        return (files("cpanel_doctor") / "assets" / name).read_text(encoding="utf-8")
    except (ImportError, AttributeError):  # Python 3.8 fallback
        import cpanel_doctor

        path = os.path.join(os.path.dirname(cpanel_doctor.__file__), "assets", name)
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
