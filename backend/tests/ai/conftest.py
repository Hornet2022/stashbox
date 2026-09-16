"""CP3.5-pre-1 LLM 单测 fixture：把 ai-service 目录加进 sys.path。

ai-service 目录名带连字符（不是合法包名），`llm` 包只能这样被 import
（做法同 tests/observability、tests/gateway 按文件路径加载服务代码）。
"""
import sys
from pathlib import Path

AI_SERVICE_DIR = Path(__file__).resolve().parents[2] / "ai-service"

if str(AI_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE_DIR))
