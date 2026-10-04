"""扣费权必须原子认领，否则并发下同一篇文章扣两次（2026-10 回归）

扣费判定的组合是 `has_article_quota`（EXISTS）+ `mark_article_quota`（无条件 SET），
合起来是**非原子的 check-then-act**：两个并发请求都能看到「还没扣过」，
于是各 `consume` 一次（`consume` 自己 commit），用户为一篇文章付两次钱；
后到的那个再撞 `distilled_articles.article_id` 唯一约束变成 500，
而多扣的那一次没人退还 —— `refund` 只在 `distill_failed` 触发。

这不是纯理论：ai-service 的代码注释里记着实测「安卓在 POST /articles 之后又调了
一次本端点（相隔 163ms）」。那个**顺序**重入由 `IN_FLIGHT_DISTILL_STATUSES`
入队守卫挡住了（此时 DB 行已存在），但「两个请求都还没提交」的并发窗口依然敞着。

修法：`claim_article_quota` 用 `SET NX` 把「判定 + 占位」压成一条命令，
只有赢家扣费；扣费失败用 `clear_article_quota` 还回认领权。

## 为什么这个文件在 tests/common 而不是 tests/ai

`tests/ai/conftest.py` 有个 autouse fixture 把 `redis.asyncio.Redis` 整个换成
`_AlwaysUnlocked`（任何 `set` 都返回 True），为了让退款幂等锁的测试跨用例干净。
而本文件要验的恰恰是 `SET NX` 的**真实**仲裁语义 —— 在那个桩下 10 个并发会全部
返回 True，测试会「红」但红得毫无意义。被测对象 `cache_service` 本身在
`common/`，`tests/common` 也没有那个 conftest，所以放这里。

接线断言只读源码文本、**不 import 服务**：import `ai-service/main.py` 需要
`llm` / `distill` 这些连字符目录的包路径，那正是 `tests/ai/conftest.py` 干的活，
在这里重复一遍只会让两个 conftest 打架。
"""

import asyncio
import uuid
from pathlib import Path

import pytest

from stashbox.backend.common import cache_service

AI_SERVICE_MAIN = Path(__file__).resolve().parents[2] / "ai-service" / "main.py"


@pytest.fixture
def article_id():
    return f"art_claim_{uuid.uuid4().hex[:20]}"


# ---------------------------------------------------------------------------
# A. 认领原语：并发下只有一个赢家
# ---------------------------------------------------------------------------


async def test_并发认领只有一个赢家(article_id):
    """10 个并发抢同一篇文章的扣费权，恰好 1 个 True。

    这是整件事的核心：原来的 EXISTS-then-SET 在这里会返回 10 个 True
    （都先看到「没扣过」）。判据用**计数**而不是布尔，正是为了让「多赢家」
    这种失败一眼可见 —— 只断言「至少一个 True」的话，全赢家也能过。
    """
    results = await asyncio.gather(
        *(cache_service.claim_article_quota(article_id) for _ in range(10))
    )
    assert results.count(True) == 1, f"应恰好 1 个赢家，实际 {results.count(True)} 个"
    await cache_service.clear_article_quota(article_id)


async def test_认领后_存在性为真(article_id):
    """认领成功 → `has_article_quota` 为真。

    预判读和仲裁写必须落在**同一个 key** 上：两者一旦分家，就会出现
    「预判说没扣过、但仲裁抢不到」这种要靠读代码才发现的错配。
    """
    assert await cache_service.claim_article_quota(article_id) is True
    assert await cache_service.has_article_quota(article_id) is True
    await cache_service.clear_article_quota(article_id)


async def test_已认领过则认领失败(article_id):
    """顺序调用两次：第一次 True、第二次 False。"""
    assert await cache_service.claim_article_quota(article_id) is True
    assert await cache_service.claim_article_quota(article_id) is False
    await cache_service.clear_article_quota(article_id)


async def test_还回认领权后可以再认领(article_id):
    """`clear_article_quota` 之后必须能重新认领 —— 扣费失败要靠它回滚。"""
    assert await cache_service.claim_article_quota(article_id) is True
    await cache_service.clear_article_quota(article_id)
    assert await cache_service.claim_article_quota(article_id) is True
    await cache_service.clear_article_quota(article_id)


async def test_不同文章互不影响():
    a, b = f"art_a_{uuid.uuid4().hex[:8]}", f"art_b_{uuid.uuid4().hex[:8]}"
    assert await cache_service.claim_article_quota(a) is True
    assert await cache_service.claim_article_quota(b) is True
    await cache_service.clear_article_quota(a)
    await cache_service.clear_article_quota(b)


async def test_还回不存在的认领不报错():
    """`clear` 对不存在的 key 是幂等的 —— 扣费失败路径不该自己再抛一次。"""
    await cache_service.clear_article_quota(f"art_none_{uuid.uuid4().hex[:16]}")


# ---------------------------------------------------------------------------
# B. 端点接线：两个扣费入口都走认领，且失败都还回
# ---------------------------------------------------------------------------


def test_两个扣费入口都用认领():
    """两个入口都走 `claim_article_quota`，且扣费失败都还回认领权。

    漏掉任何一个入口 = 留一半的重复扣费窗口，而这类缺口在 e2e 里看不出来
    （e2e 不会并发打同一个 article_id），只能靠源码断言钉住。
    """
    text = AI_SERVICE_MAIN.read_text()
    assert text.count("claim_article_quota(") >= 2, "两个扣费入口都要改成认领"
    assert text.count("clear_article_quota(") >= 2, "扣费失败必须还回认领权，否则用户白送"
    # 旧的两步写法不该再留在入口里
    assert "mark_article_quota(article_id)" not in text, "入口里还有无条件 SET 的旧写法"
    assert "mark_article_quota(req.article_id)" not in text, "入口里还有无条件 SET 的旧写法"


def test_认领必须用_nx():
    """`SET NX` 才是原子仲裁；写成普通 `SET` 就退回 check-then-act 了。"""
    src = (Path(cache_service.__file__)).read_text()
    start = src.index("async def claim_article_quota(")
    body = src[start : src.index("async def clear_article_quota(")]
    assert "nx=True" in body, "认领没用 SET NX —— 并发下会有多个赢家"
    assert "ex=86400" in body, "认领必须带 TTL，否则失败的认领会永久挡住后续扣费"
