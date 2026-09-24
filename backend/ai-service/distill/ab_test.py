"""CP5.6.0 §2.7 D：A/B 测试基础设施。

按 docs/听感产品化方案_v1.md §2.7 D 严格实现：
- 分流：user_id % 100 < 30 → 个性化组；其余 → 通用组
- 30% / 70% 设计（保守）
- 指标：复听率 / 完听率 / 评分均值 / 取消订阅率（CP5.6.x 后续接）
"""

from __future__ import annotations

from typing import Literal

import structlog

log = structlog.get_logger("distill.ab_test")

# 分流阈值
_PERSONALIZATION_THRESHOLD = 30
_TOTAL_BUCKETS = 100


class ABTest:
    """CP5.6.0 §2.7 D：A/B 测试基础设施。"""

    def is_personalization_group(self, user_id: int) -> bool:
        """CP5.6.0 §2.7 D：分流 user_id % 100 < 30 → 个性化组。"""
        bucket = user_id % _TOTAL_BUCKETS
        return bucket < _PERSONALIZATION_THRESHOLD

    def assign_group(self, user_id: int) -> Literal["personalized", "general"]:
        """CP5.6.0 §2.7 D：返回 A/B 组。"""
        if self.is_personalization_group(user_id):
            log.info("ab_test_assigned", user_id=user_id, group="personalized")
            return "personalized"
        log.info("ab_test_assigned", user_id=user_id, group="general")
        return "general"
