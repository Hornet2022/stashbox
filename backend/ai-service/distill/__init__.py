"""蒸馏 4 步流水线（CP3.5-pre-2，v1 §5 L4 蒸馏引擎）。

用法::

    from distill import DistillContext, DistillPipeline, MockTTSClient
    from llm import get_llm_client

    ctx = DistillContext(task_id=..., article_id=..., user_id=..., url=..., raw_content=...)
    pipeline = DistillPipeline(get_llm_client())
    await pipeline.run(ctx)

ai-service 目录名带连字符（不是合法包名），这里把它加进 sys.path ——
`llm` / `config_llm` 因此作为顶层模块导入（做法同 llm/__init__.py）。
"""
import sys
from pathlib import Path

_AI_SERVICE_DIR = str(Path(__file__).resolve().parent.parent)
if _AI_SERVICE_DIR not in sys.path:
    sys.path.insert(0, _AI_SERVICE_DIR)

from .mock_tts import MockTTSClient  # noqa: E402
from .pipeline import DistillPipeline  # noqa: E402
from .schemas import (  # noqa: E402
    AudioConcatOutput,
    DistillContext,
    RewriteOutput,
    StructuredChapter,
    StructuredOutput,
    TTSOutput,
)
from .state_machine import (  # noqa: E402
    TRANSITIONS,
    DistillStatus,
    can_transition,
    transition,
)
from .steps import step1_structure, step2_rewrite, step3_tts, step4_concat  # noqa: E402

__all__ = [
    "DistillPipeline",
    "MockTTSClient",
    "DistillContext",
    "StructuredChapter",
    "StructuredOutput",
    "RewriteOutput",
    "TTSOutput",
    "AudioConcatOutput",
    "DistillStatus",
    "TRANSITIONS",
    "can_transition",
    "transition",
    "step1_structure",
    "step2_rewrite",
    "step3_tts",
    "step4_concat",
]
