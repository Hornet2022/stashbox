"""CP5.6.0 §2.8 / §3.1：隐私服务（GDPR / 跨用户金句 / 数据删除）。

按 docs/听感产品化方案_v1.md §2.8 + §3.1 严格实现：
- delete_user_data: GDPR 账号注销时清理（user_listening_patterns + few_shot_examples + consents）
- should_share_across_users: 跨用户金句复用决策
  - 默认 False（仅结构复用，金句不跨用户）
  - 未成年 + free 用户禁用
"""

from __future__ import annotations

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.models import FewShotExample, UserListeningPattern

log = structlog.get_logger("distill.privacy_service")


class PrivacyService:
    """CP5.6.0 §2.8：隐私服务。"""

    async def delete_user_data(self, db: AsyncSession, user_id: int) -> int:
        """CP5.6.0 §2.8 GDPR：账号注销时清理听感画像 + 个人 few-shot + 同意记录。

        Returns:
            删除的总记录数
        """
        try:
            total = 0
            # 1. 删 user_listening_patterns
            pat_result = await db.execute(
                delete(UserListeningPattern).where(UserListeningPattern.user_id == user_id)
            )
            pat_count = pat_result.rowcount or 0
            total += pat_count

            # 2. 删 user_id = user_id 的 few_shot_examples（个人池）
            fs_result = await db.execute(
                delete(FewShotExample).where(FewShotExample.user_id == user_id)
            )
            fs_count = fs_result.rowcount or 0
            total += fs_count

            # 3. 删 user_consents（如果表存在，CP5.6.0 加）
            try:
                from stashbox.backend.common.models import ConsentRecord

                consent_result = await db.execute(
                    delete(ConsentRecord).where(ConsentRecord.user_id == user_id)
                )
                consent_count = consent_result.rowcount or 0
                total += consent_count
            except ImportError:
                # CP5.6.0 前表不存在，跳过
                pass

            await db.commit()
            log.info(
                "privacy_delete_user_data",
                user_id=user_id,
                patterns=pat_count,
                few_shots=fs_count,
                total=total,
            )
            return total
        except Exception as e:
            await db.rollback()
            log.warning(
                "privacy_delete_user_data_failed",
                user_id=user_id,
                error=str(e),
            )
            return 0

    def should_share_across_users(
        self,
        user_tier: str | None = None,
        is_minor: bool = False,
    ) -> bool:
        """CP5.6.0 §2.8 决策：跨用户金句复用（默认仅结构复用）。

        规则：
        1. is_minor = True → False（未成年禁用）
        2. user_tier = 'free' → False（免费层金句不复用）
        3. 默认 False（保守：仅结构复用，金句不跨用户）
        """
        if is_minor:
            log.info("privacy_share_across_users_minor_blocked")
            return False
        if user_tier == "free":
            log.info("privacy_share_across_users_free_blocked")
            return False
        # CP3.7.3 + CP5.6.0 默认行为：仅结构复用
        log.info("privacy_share_across_users_default_struct_only")
        return False
