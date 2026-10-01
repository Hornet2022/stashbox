"""CP-AGENT-MEMORY：听匣 agent 的 memory store。

Phase 2 引入。memory 在 agent 启动时从 PG 读入到 AgentState.user_profile /
few_shot_examples，节点把 memory 注入到 LLM prompt。

3 类 memory：
  UserProfile     — 用户级（tier / preferences / cohort / AB group）
                    存 users 表（已有）+ 未来 user_preferences 表
  FewShotExamples — 全局级（按 topic / voice / style 分桶）
                    存 few_shot_examples 表（已有）
  PipelineMemory  — 单次任务级（fetched_content / rewritten_script）
                    存 AgentState 字段（自动）

读取策略：
  - UserProfile：每个 distill run 都读一次（read-through，不缓存）
  - FewShotExamples：每 5 分钟缓存一次（CP11.x 与 distill pipeline 行为一致）
  - PipelineMemory：始终在 state 上，不读库

设计原则：memory store 只读 + 注入，不写。写 memory 走 ToolRegistry 的 save_memory tool
（agent 决策后调），避免 LLM 误调写操作导致数据污染。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("agent.memory")

# few_shot_examples.kind 的合法取值（表上有 CHECK 约束，见 common/models/few_shot_example.py）。
# 写在这里是为了让 load_few_shots 能在查询前就挡住非法值 —— 之前就是缺这道校验，
# 传了个非法值进去，查询静默返回空集，没人发现。
_FEW_SHOT_KINDS = frozenset({"hook", "section", "outro"})

# score_avg 的分制。few_shot_examples 上有 CHECK (score_avg BETWEEN 1 AND 5)，
# 不是 10 分制 —— 之前 prompt 里写「评分 x/10」会误导 LLM。
_FEW_SHOT_SCORE_MAX = 5


@dataclass(frozen=True)
class UserProfile:
    """CP-AGENT-MEMORY-USER：用户级 profile。"""

    user_id: int
    tier: str  # free / pro / member / admin / operator
    ab_group: Optional[str] = None  # CP-B4 A/B 实验分组（NULL=未实验期）
    preferences: dict[str, Any] = field(default_factory=dict)
    """个性化偏好（Phase 2.5 接入 user_preferences 表，Phase 1 为空 dict）。"""

    def to_prompt_fragment(self) -> str:
        """把 profile 序列化成可注入 LLM prompt 的片段。"""
        lines = [
            f"用户等级：{self.tier}",
        ]
        if self.ab_group:
            lines.append(f"A/B 分组：{self.ab_group}")
        if self.preferences:
            for k, v in self.preferences.items():
                lines.append(f"偏好·{k}：{v}")
        return "\n".join(lines)


@dataclass(frozen=True)
class FewShotExample:
    """CP-AGENT-MEMORY-FEW-SHOT：单个 few-shot 样本。"""

    input_excerpt: str  # 原文章片段（≤ 200 字）
    output_excerpt: str  # 改写后片段（≤ 200 字）
    score: float = 0.0  # 历史评分（0-10，越高越好）
    tags: tuple[str, ...] = ()  # 主题标签 ["科技", "商业"]

    def to_prompt_pair(self) -> str:
        return f"[输入]\n{self.input_excerpt}\n[输出]\n{self.output_excerpt}"


class MemoryStore:
    """CP-AGENT-MEMORY-STORE：听匣 agent 的 memory 访问入口。

    用法：
      store = MemoryStore(db_session_factory)
      profile = await store.load_user_profile(user_id)
      examples = await store.load_few_shots(kind="hook", limit=5)   # kind ∈ hook/section/outro
    """

    def __init__(self, session_factory: Optional[Any] = None) -> None:
        self._session_factory = session_factory
        self._few_shot_cache: dict[tuple[str | None, int], tuple[float, list[FewShotExample]]] = {}
        """Few-shot cache: key=(topic, limit) → (ts, examples)。TTL 300s。"""

    async def load_user_profile(self, user_id: int) -> UserProfile:
        """CP-AGENT-MEMORY-USER-LOAD：读用户等级 + **真实听感画像**。

        2026-10-02 补：原来这里只 `SELECT id, tier FROM users`，preferences 恒为
        空 dict，于是 `to_prompt_fragment()` 吐出来只有一行「用户等级：member」——
        画像表 user_listening_patterns 明明在写，却没人读。现在 join 进来。

        注意：`ab_group`（A/B 实验分组）在 distilled_articles 表上，不在 users 表。
        用户级 A/B 分组按 user_id % 100 < 30 规则在 distill 时计算（见 CP-B4 方案 §2.7-D），
        这里不查 ab_group（避免 UndefinedColumnError）。

        防御：users 表或画像表可能缺列（老 schema），查询失败时回落 tier="free"。
        """
        if self._session_factory is None:
            log.warning("memory_load_user_profile_no_session_factory")
            return UserProfile(user_id=user_id, tier="free")

        from sqlalchemy import text

        try:
            async with self._session_factory() as db:
                row = (
                    await db.execute(
                        text(
                            "SELECT u.id, u.tier, "
                            "  p.feedback_count, p.avg_overall_score, "
                            "  p.preferred_rhythm, p.preferred_hook_type, "
                            "  p.skip_rate, p.completion_rate, p.avg_session_sec "
                            "FROM users u "
                            "LEFT JOIN user_listening_patterns p "
                            "  ON p.user_id = u.id AND p.deleted_at IS NULL "
                            "WHERE u.id = :uid"
                        ),
                        {"uid": user_id},
                    )
                ).first()
        except Exception as exc:
            # CP-AGENT-MEMORY-USER-LOAD-DEFENSIVE：读 profile 失败不应阻断蒸馏
            log.warning(f"memory_load_user_profile_failed user_id={user_id} error={exc!s}")
            return UserProfile(user_id=user_id, tier="free")

        if not row:
            return UserProfile(user_id=user_id, tier="free")

        return UserProfile(
            user_id=int(row.id),
            tier=str(row.tier or "free"),
            ab_group=None,  # 见 docstring：用户级 ab_group 不在此表
            preferences=_build_preferences(row),
        )

    async def load_few_shots(
        self,
        kind: Optional[str] = None,
        limit: int = 5,
    ) -> list[FewShotExample]:
        """CP-AGENT-MEMORY-FEW-SHOT-LOAD：从 few_shot_examples 表读样本。

        真实表列：source_pattern / rewrite_text / score_avg / kind / active。
        映射到 FewShotExample：
          source_pattern → input_excerpt
          rewrite_text   → output_excerpt
          score_avg      → score（**1-5 分制**，见表上 CHECK 约束）
          kind           → tags（单元素 tuple）

        ## ⚠️ kind 参数曾经是个静默失效的坑（2026-10-02 修）

        原签名是 ``load_few_shots(topic=...)``，而实现把它直接塞进
        ``WHERE kind = :t``。调用方 ``distill_task.py`` 传的却是**文章来源**
        （``wechat`` / ``web`` / ``douyin``…），而 ``kind`` 列有 CHECK 约束
        只能是 ``hook`` / ``section`` / ``outro`` —— 于是这个查询**恒返回 0 行，
        而且不报错**。表现是「few-shot 池子只增不读，个性化像没上线」。

        所以现在参数名直接叫 ``kind``，取值域写死在下面；不再拿 topic 冒充
        kind。主题维度的检索（表里没有主题列，source_pattern 实际存的是
        task_id 的 hash）留待有真实主题标签时再加。

        防御：表缺列 / 查询失败 → 返回 []（few-shot 缺失不阻断蒸馏）。
        """
        import time

        if kind is not None and kind not in _FEW_SHOT_KINDS:
            log.warning("memory_load_few_shots_bad_kind kind=%r 合法值=%s", kind, _FEW_SHOT_KINDS)
            return []

        cache_key = (kind, limit)
        cached = self._few_shot_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < 300:
            return cached[1]

        if self._session_factory is None:
            return []

        from sqlalchemy import text

        try:
            async with self._session_factory() as db:
                if kind:
                    rows = (
                        await db.execute(
                            text(
                                "SELECT source_pattern, rewrite_text, score_avg, kind "
                                "FROM few_shot_examples "
                                "WHERE active = true AND kind = :k "
                                "ORDER BY score_avg DESC LIMIT :n"
                            ),
                            {"k": kind, "n": limit},
                        )
                    ).all()
                else:
                    rows = (
                        await db.execute(
                            text(
                                "SELECT source_pattern, rewrite_text, score_avg, kind "
                                "FROM few_shot_examples "
                                "WHERE active = true "
                                "ORDER BY score_avg DESC LIMIT :n"
                            ),
                            {"n": limit},
                        )
                    ).all()
        except Exception as exc:
            # CP-AGENT-MEMORY-FEW-SHOT-DEFENSIVE：表缺列 / 空表不应阻断蒸馏
            log.warning(f"memory_load_few_shots_failed kind={kind} error={exc!s}")
            return []

        examples = [
            FewShotExample(
                input_excerpt=r.source_pattern or "",
                output_excerpt=r.rewrite_text or "",
                score=float(r.score_avg or 0),
                tags=(r.kind,) if r.kind else (),
            )
            for r in rows
        ]

        self._few_shot_cache[cache_key] = (time.time(), examples)
        return examples

    def inject_into_prompt(
        self,
        profile: Optional[UserProfile],
        examples: list[FewShotExample],
        base_prompt: str,
    ) -> str:
        """CP-AGENT-MEMORY-INJECT：把 memory 注入到 LLM prompt。"""
        parts: list[str] = []

        if profile and (profile.tier != "free" or profile.preferences):
            parts.append("# 用户偏好\n" + profile.to_prompt_fragment())

        if examples:
            parts.append("# Few-shot 样本（高质量历史改写）")
            for idx, ex in enumerate(examples, 1):
                parts.append(
                    f"## 样本 {idx}（评分 {ex.score:.1f}/{_FEW_SHOT_SCORE_MAX}）\n"
                    f"{ex.to_prompt_pair()}"
                )

        if not parts:
            return base_prompt

        return "\n\n".join(parts) + "\n\n# 当前任务\n" + base_prompt


def _build_preferences(row: Any) -> dict[str, Any]:
    """把画像行压成可直接进 prompt 的偏好 dict（只保留真实有值的字段）。

    刻意不塞占位值 —— 画像表里 avg_session_sec / skip_rate / completion_rate
    三列至今没有任何写入方（恒 NULL），写进 prompt 等于告诉 LLM「这个用户
    跳过率是 0」，比不给更糟。宁缺毋滥。
    """
    prefs: dict[str, Any] = {}

    if row.feedback_count:
        prefs["已反馈条数"] = int(row.feedback_count)
    if row.avg_overall_score is not None:
        prefs["平均听感评分"] = f"{float(row.avg_overall_score):.1f}/5"
    if row.preferred_rhythm:
        prefs["偏好节奏"] = str(row.preferred_rhythm)
    if row.preferred_hook_type:
        prefs["偏好开场"] = str(row.preferred_hook_type)
    for col, label in (
        ("skip_rate", "跳过率"),
        ("completion_rate", "完听率"),
        ("avg_session_sec", "平均单次收听秒数"),
    ):
        val = getattr(row, col, None)
        if val is not None:
            prefs[label] = round(float(val), 3)

    return prefs
