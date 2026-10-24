"""蒸馏状态 —— **对外 5 态的唯一一份实现**（读侧派生 + 写侧终局失败落库）。

## 为什么要有这个模块

同一个问题「这篇稿子现在什么状态」，仓里曾经有**两份实现**：

- `content-service/main.py::_derive_status` —— 文章详情 / 状态端点用
  （`articles` LEFT JOIN `distilled_articles` 后派生）
- `ai-service/main.py::_external_distill_status` —— 任务状态端点用
  （只看 `distilled_articles.status` 的内部 7 态 → 对外 5 态映射）

两份实现的**输入不同**（一份看两表，一份只看产物表），于是同一个状态在两个
端点上会给出**不同的答案**。2026-10-25 实测到的三个具体后果：

1. **终局失败时两个端点给不同答案** ——
   失败路径只写 `articles.status='failed'`；产物表在**非 agent 失败路径**上
   停在 `queued`（agent 路径的 `_persist_agent_final` 会写成 'failed'，
   但 ai-service 的旧实现**只看产物表**、看不到 `articles.status`）。
   ⇒ 非 agent 失败：任务端点 `pending`、文章端点 `failed`；
   agent 失败：两边都 `failed`（这次碰巧一致 —— 靠的是「两边都看产物表」，
   不是同一份判据；换个组合又会对不上）。
2. **重试中间态两端口径差一档** —— 产物表是 `failed` 时，ai-service 映射成
   `failed`，而 content-service 按 `articles.status` 给 `distilling`
   （用户其实正在合成语音）。App 轮询的是 content-service 那个，
   于是同一个任务在两个端点上「进度」不一致。
3. **「用户点了重试还是看到失败」** —— 产物表已是 `failed`、但
   `articles.status` 被重试入口置回 `pending`，content-service 会给 `pending`
   （对的），ai-service 却按产物表给 `failed`（错的）。

⇒ 收口到**一份**实现：两个端点都用 `derive_distill_status(art, task)`，
输入一样、输出必然一样。

## 口径

| 情况 | 对外状态 |
|---|---|
| 产物表 `done` | `ready` |
| 产物表 `failed` + `articles.status ∈ {pending, distilling}` | `articles.status`（**还在重试**，不是终态） |
| 产物表 `failed` + `articles.status` 已是终态 | `failed` |
| 其他（产物表 `queued` / `step1~4` / 未知） | `articles.status`（进行中） |
| 没有产物行 | `articles.status` |

## 为什么以 `articles.status` 为准

ai-service 的**成功**路径才写 `articles.status`；失败路径以产物表为准。
而产物表的 `failed` 只是一次尝试的结果：

- Arq 自动重试：一轮失败 → 产物表先 failed → 15 秒后重试**成功**
  （内测实测一篇稿子 LLM 返回空 content，首轮失败、重试成功）。
  用户截到的正是中间态那张「失败 / 换个源试试？」的页面 ——
  所以中间态不能显示 failed。
- 用户手动点「重试蒸馏」：`articles.status` 被置回 `pending`，
  产物表要等 ai-service 回写新任务才更新。

⇒ `articles.status` 说「还在进行」就是还在进行；两者不一致时**以 articles 为准**。

## 为什么产物表的失败要晚一步

**失败分支**只在终局失败时写产物表的 `failed`；中间态（Arq 还会重试）不写。
理由：中间态写 failed 会让用户提前看到「失败」页，然后点「重试蒸馏」→
白烧一次 LLM+TTS。

⚠️ 有一个例外必须说清楚，否则这份文档本身就在骗人：agent 路径的
`_persist_agent_final` 在 agent 返回非 done 时**当场**就把产物表写成
`failed`（那是它的落库时机，早于失败分支跑）。这不影响对外口径 ——
读侧规则「产物表 failed + articles 还在进行 ⇒ 以 articles 为准」正好覆盖
它（用户看到的是「正在重试」，不是「失败」）。但「产物表中间态一定是
queued」这种说法是错的。

反过来，**终局失败必须写**产物表，否则有两个真问题：

1. **用户的「重试蒸馏」按钮按下去没反应** —— 入队幂等守卫
   （`ai-service/main.py`）只挡产物表状态为 `queued`/`running` 的行。
   产物表停在 `queued` 时，守卫会一直挡到陈旧兜底窗口
   （`STALE_DISTILL_TASK_SEC`，默认 900s）才放行 ⇒ 用户点了重试，
   15 分钟里什么都没发生（实测症状）。
2. **admin 统计系统性低估失败率** —— `failed_distillations_24h` 与
   `distill_success_rate` 都按 `DistilledArticle.status == 'failed'` 统计，
   而非 agent 路径的失败（正文为空、抓取失败等）从不落这个值。

⇒ 写侧的「终局失败落库」统一走 [`mark_distill_task_failed`]，
两个失败分支（正文为空早退 / 异常终局）都调它 —— **不各写一份**。

## 未知值为什么降级成 `distilling`

DB 被人工改过、或状态机引入了新中间态时，派生结果可能不在对外枚举里。
客户端（安卓 `DistillStatus.kt`）对状态是**强校验**的 ⇒ 遇到未知值会崩。
所以这里兜底成 `distilling`（"还在进行"，最保守的合法值）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import update

from .models import DistilledArticle

if TYPE_CHECKING:  # 运行期不 import（避免和 models 形成环）
    from sqlalchemy.ext.asyncio import AsyncSession

    from .models import Article

# ── 对外契约状态（客户端强校验枚举） ─────────────────────────────────────
EXTERNAL_PENDING = "pending"
EXTERNAL_DISTILLING = "distilling"
EXTERNAL_READY = "ready"
EXTERNAL_FAILED = "failed"
EXTERNAL_LISTENED = "listened"

EXTERNAL_STATUSES = frozenset(
    {
        EXTERNAL_PENDING,
        EXTERNAL_DISTILLING,
        EXTERNAL_READY,
        EXTERNAL_FAILED,
        EXTERNAL_LISTENED,
    }
)

# 判定「还在进行」的两种 articles.status：
#   · pending  —— 用户点了「重试蒸馏」，入口把它置回 pending
#   · distilling —— Arq 正在重试
# 这两种情况下产物表的 failed 只是「上一次尝试的结果」，不是终态。
_IN_PROGRESS_ARTICLE_STATUSES = frozenset({EXTERNAL_PENDING, EXTERNAL_DISTILLING})


def derive_distill_status(
    art: "Article",
    task: "DistilledArticle | None",
) -> str:
    """文章 + 蒸馏任务 → 对外状态（5 态）。

    这是**唯一一份**实现：content-service 的文章状态端点与
    ai-service 的任务状态端点都用它 ⇒ 两端口径必然一致。

    Args:
        art: `articles` 行（状态权威来源）
        task: `distilled_articles` 行（没有就是 None）

    Returns:
        `pending` / `distilling` / `ready` / `failed` / `listened` 之一；
        派生结果不在枚举里时降级成 `distilling`（见模块 docstring）。
    """
    if task is None:
        return _safe(art.status)

    if task.status == "done":
        return EXTERNAL_READY

    if task.status == "failed":
        # 产物表的 failed 只说明「上一次尝试失败了」。
        # articles 还在进行 ⇒ 还在重试 ⇒ 不是终态（用户不该看到「失败」页）。
        if art.status in _IN_PROGRESS_ARTICLE_STATUSES:
            return _safe(art.status)
        return EXTERNAL_FAILED

    # 其余（queued / step1~4 / 未知）都是「进行中」，以 articles.status 为准。
    return _safe(art.status)


async def mark_distill_task_failed(
    db: "AsyncSession",
    article_id: str,
    *,
    clear_audio: bool = True,
) -> None:
    """产物表**终局失败**落 `failed`（与 `articles.status='failed'` 同一事务）。

    ⚠️ **只在终局失败时调**。中间态（Arq 还会重试）调它会让用户提前看到
    「失败」页 ⇒ 白点一次「重试蒸馏」⇒ 白烧一次 LLM+TTS（见模块 docstring）。

    Args:
        db: 调用方的 session（**同一事务** ⇒ 产物表与 articles 状态同时可见）
        article_id: 目标文章
        clear_audio: 是否同时清掉 `audio_url` / `duration_sec`。
            终局失败 = 本次没有产出 ⇒ 行上不该留着上一轮的音频地址
            （`status='failed'` 却挂着可播的 URL 是自相矛盾；
            mock 路径 `_run_pipeline` 早就这么收口了）。
            旧音频文件本身不删 —— 留给 GC（见 distill_task 的说明）。
    """
    values: dict[str, object] = {"status": EXTERNAL_FAILED}
    if clear_audio:
        values["audio_url"] = None
        values["duration_sec"] = None

    await db.execute(
        update(DistilledArticle)
        .where(
            DistilledArticle.article_id == article_id,
            # ⚠️ 不覆盖 'done'：重跑时产物表被 _requeue_or_create 置回 'queued'
            # （故意不清旧产物字段），但若这一行已经 'done'，那说明有一轮
            # **成功**过 —— 不能被后一次失败打成 failed。
            DistilledArticle.status != "done",
        )
        .values(**values)
    )


def _safe(status: str | None) -> str:
    """把状态收敛到对外枚举内（未知值降级成 `distilling`）。"""
    if status and status in EXTERNAL_STATUSES:
        return status
    return EXTERNAL_DISTILLING
