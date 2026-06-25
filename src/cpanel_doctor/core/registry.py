"""Discovery of available patches.

Adding a new fix is just dropping a ``Patch`` subclass into
``cpanel_doctor.patches``; it is picked up automatically.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Dict, List

from .patch import Patch


def load_patches() -> List[Patch]:
    import cpanel_doctor.patches as patches_pkg

    found: List[Patch] = []
    for mod_info in pkgutil.iter_modules(patches_pkg.__path__):
        module = importlib.import_module(f"{patches_pkg.__name__}.{mod_info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Patch) and obj is not Patch and obj.__module__ == module.__name__:
                instance = obj()
                if getattr(instance, "id", ""):
                    found.append(instance)
    found.sort(key=lambda p: p.id)
    return found


def patches_by_id() -> Dict[str, Patch]:
    return {p.id: p for p in load_patches()}
