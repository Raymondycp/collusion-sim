"""Plug-in loading: every module in a package registers one plug-in when imported. One pattern for all registries."""

from __future__ import annotations

import importlib
import pkgutil


def load_all(package: str) -> list[str]:
    """Import each module of `package` (sorted by name, so the registry order is stable). Returns the module names."""
    pkg = importlib.import_module(package)
    names = sorted(m.name for m in pkgutil.iter_modules(pkg.__path__))
    for name in names:
        importlib.import_module(f"{package}.{name}")
    return names
