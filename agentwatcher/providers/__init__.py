"""Provider registry: built-ins (toggled via config) plus user drop-ins.

A provider is any module with `scan(state, cfg) -> list[Session]`. Drop a .py
file into ~/.config/agentwatcher/providers/ to add support for a new harness
without touching this package.
"""
import glob
import importlib
import importlib.util
import os

from agentwatcher.core import USER_PROVIDER_DIR

BUILTIN = ["claude_code", "codex"]


def load_providers(cfg):
    providers = []
    for name in BUILTIN:
        if cfg["providers"].get(name, {}).get("enabled", True):
            providers.append((name, importlib.import_module(f"agentwatcher.providers.{name}").scan))
    for path in sorted(glob.glob(os.path.join(USER_PROVIDER_DIR, "*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            spec = importlib.util.spec_from_file_location(f"agentwatcher.user.{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if callable(getattr(mod, "scan", None)):
                providers.append((name, mod.scan))
        except Exception as e:
            print(f"agentwatcher: failed to load provider {path}: {e}")
    return providers
