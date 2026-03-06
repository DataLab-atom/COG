import sys

from . import runtime_utils as _runtime_utils

# Ensure the legacy module name `utils` keeps working everywhere.
if "utils" not in sys.modules:
    sys.modules["utils"] = _runtime_utils

__all__ = ["runtime_utils", "code_search_agent", "paper_search_agent"]


def __getattr__(name: str):
    if name in {"code_search_agent", "paper_search_agent"}:
        import importlib

        module = importlib.import_module(f"{__name__}.{name}")
        setattr(sys.modules[__name__], name, module)
        return module
    raise AttributeError(name)
