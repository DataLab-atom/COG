# mcts 包初始化 — 将 open-agents 加入 sys.path
import sys
from pathlib import Path

_OA = str(Path(__file__).parent.parent.parent / "open-agents")
if _OA not in sys.path:
    sys.path.insert(0, _OA)
