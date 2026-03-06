from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from LLM_MODULE.tools import TOOL_MANAGER, dispatch_tool, load_plugins, tool_spec


def main() -> None:
    #print("[before] manifest:", TOOL_MANAGER.manifest_json())
    #loaded = load_plugins(on_error="raise")
    #print("[plugins] loaded:", loaded)
    #print("[after] manifest:", TOOL_MANAGER.manifest_json())

    print("[math toolset spec]:", [t["function"]["name"] for t in tool_spec(toolset="dataset_coder")])
    #print("[call add]:", dispatch_tool("add", {"a": 1, "b": 2}, toolset="math", enforce_policy=True))


if __name__ == "__main__":
    main()
