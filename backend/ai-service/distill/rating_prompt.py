"""CP5.6.1 §3.1：评分请求推送策略。

按 docs/听感产品化方案_v1.md §3.1 严格实现：
- 4 档强度：strong / medium / weak / none
- 规则（按 feedback_count）：
  - 0-2: strong (新用户首篇 + 前 3 篇)
  - 3-4: medium (第 4-5 篇，再强化)
  - 5-19: weak (5 篇后偶尔)
  - 20+: none (画像已稳，不再打扰)
- 限频：每次推送间隔 >= 24h
"""

from __future__ import annotations

import structlog
from datetime import datetime, timedelta
from typing import Literal

log = structlog.get_logger("distill.rating_prompt")

PromptIntensity = Literal["strong", "medium", "weak", "none"]

# 强度档位阈值
_STRONG_MAX = 2  # 0-2 strong
_MEDIUM_MAX = 4  # 3-4 medium
_WEAK_MAX = 19  # 5-19 weak

# 限频：24h
_MIN_INTERVAL_HOURS = 24


class RatingPromptStrategy:
    """CP5.6.1 §3.1：评分请求推送策略。"""

    def get_prompt_intensity(
        self,
        feedback_count: int,
    ) -> PromptIntensity:
        """CP5.6.1：评分请求强度。"""
        if feedback_count <= _STRONG_MAX:
            return "strong"
        if feedback_count <= _MEDIUM_MAX:
            return "medium"
        if feedback_count <= _WEAK_MAX:
            return "weak"
        return "none"

    def should_show_prompt(
        self,
        feedback_count: int,
        last_prompt_at: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        """CP5.6.1：判断是否应该弹出评分请求（限频）。

        Args:
            feedback_count: 用户已有反馈数
            last_prompt_at: 上次推送时间（None = 从未推送）
            now: 当前时间（默认 now()）

        Returns:
            True if should show prompt
        """
        if now is None:
            now = datetime.now()

        intensity = self.get_prompt_intensity(feedback_count)
        if intensity == "none":
            return False

        # 首次推送：总是显示
        if last_prompt_at is None:
            return True

        # 限频：距上次 >= 24h
        interval = now - last_prompt_at
        return interval >= timedelta(hours=_MIN_INTERVAL_HOURS)
