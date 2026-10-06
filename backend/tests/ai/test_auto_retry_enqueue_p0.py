"""2026-10 P0：auto_retry 这条链其实是**断**的（两个缺陷）。

## 缺陷 1：入队从未发生

`enqueue_auto_retry` 原来写的是 `from .dispatcher import Dispatcher`。但
`distill/` 下**没有** dispatcher.py —— dispatcher 在 `ai-service/dispatcher.py`
（是 `distill/` 的**兄弟**，不是子模块），类名还叫 `DistillDispatcher` 不叫
`Dispatcher`。两个错叠在一起，ImportError 必然发生。

ImportError 被同函数末尾的 `except Exception` 吞掉，只留一行
`auto_retry_enqueue_failed` warning，函数返回 None。而调用方
`hooks_impl.py:424` 的 `bump_user_daily_retry_count()` 在**调用之前**就执行了
（那里的注释明写「先占额度再入队」）。两者相加：

    用户打 1-2 星 → 扣掉当天 3 次里的一次额度 → 入队失败被吞 → 什么都没发生

净效果是：自动重蒸功能等于不存在，而用户白白损失额度。更麻烦的是 hook 那条
`auto_retry_triggered` info 日志照打不误 —— 看日志像跑通了。

**这类缺陷测试的难点**：它不报错，只是安静地什么都不做。所以判据不能是
「没抛异常」，必须是「真的产出了一个 job_id」。

## 缺陷 2：重试任务失败时退了从未扣过的配额

`distill_task` 的两个退款点此前是无条件执行的。auto_retry 走的也是同一个
`distill_task`，于是「一次都没扣过配额」的补偿重跑失败后照样退款 —— 用户白赚。

修法是显式参数 `quota_charged`，**不**靠 task_id 是否 `retry_` 前缀去嗅探：
把隐式约定当契约，换个调用方就再错一次。

下面的用例成对写（退 / 不退），只实现一半也是错的：
只加条件不传参会变成「谁都不退」= 用户白扣，比原 bug 更糟。
"""

import importlib

import pytest

from distill import auto_retry as ar


# ---------------------------------------------------------------------------
# 缺陷 1：入队必须真的发生
# ---------------------------------------------------------------------------


def test_引用的_dispatcher_模块与类名真实存在():
    """静态判据：被 import 的模块能加载，且里面有那个类。

    比「跑一遍看结果」更直接地锁住根因 —— 错模块名 / 错类名两种改法都会红。
    """
    mod = importlib.import_module("dispatcher")

    assert hasattr(mod, "DistillDispatcher"), (
        "auto_retry 导入的类名不存在：真实类名是 DistillDispatcher。"
        "错这个名字 → ImportError 被 except 吞掉 → 重试额度白烧、入队从未发生"
    )


def test_distill_目录下_没有_dispatcher_子模块():
    """锁住「为什么不能用相对导入」这个前提本身。

    写这行 `from .dispatcher import ...` 的人，脑子里的模型是 dispatcher 在
    distill/ 里。这条断言把这个误解显式钉死，免得哪天又被「修复」回去。
    """
    assert not hasattr(ar, "dispatcher") or ar.dispatcher.__name__ != "distill.auto_retry"


class _RecordingDispatcher:
    """替身：只记住被怎么调用的，然后返回一个像样的 job_id。"""

    calls: list[dict] = []

    async def enqueue_distill(self, **kwargs) -> str:
        type(self).calls.append(kwargs)
        return "job-xyz"


@pytest.fixture
def recording_dispatcher(monkeypatch):
    _RecordingDispatcher.calls = []
    dispatcher_mod = importlib.import_module("dispatcher")
    monkeypatch.setattr(dispatcher_mod, "DistillDispatcher", _RecordingDispatcher)
    yield _RecordingDispatcher
    _RecordingDispatcher.calls = []


@pytest.mark.asyncio
async def test_入队真的产生了_job_id(recording_dispatcher):
    """核心回归：ImportError 那行修好后，这里必须**非 None**。

    修复前 `enqueue_auto_retry` 因为 ImportError 被自己的 except 吞掉，返回 None。
    """
    job_id = await ar.enqueue_auto_retry(
        user_id=7, article_id="art_1", url="https://x.com/a", title="标题"
    )

    assert job_id == "job-xyz", "入队又失败了 —— 重试额度白烧，功能等于不存在"
    assert len(recording_dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_入队失败仍然返回_none_不抛(recording_dispatcher, monkeypatch):
    """反向保障：真失败时依旧降级为 None，不能把异常抛给评分写入路径。"""

    class _Boom:
        async def enqueue_distill(self, **kwargs) -> str:
            raise RuntimeError("Arq 连不上")

    import importlib as _il

    monkeypatch.setattr(_il.import_module("dispatcher"), "DistillDispatcher", _Boom)

    assert await ar.enqueue_auto_retry(user_id=7, article_id="a", url="u") is None


@pytest.mark.asyncio
async def test_重试任务声明_未扣配额(recording_dispatcher):
    """`quota_charged=False`：重试是补偿性重跑，从没扣过配额。

    这是缺陷 2 的源头 —— 传成默认的 True 时，失败路径会把从未扣过的钱退回去。
    """
    await ar.enqueue_auto_retry(user_id=7, article_id="art_1", url="https://x.com/a")

    (call,) = recording_dispatcher.calls
    assert (
        call["quota_charged"] is False
    ), "auto_retry 不扣配额，却声明成已扣 —— 失败时会退一笔从未收过的款"


@pytest.mark.asyncio
async def test_重试任务_id_是新的(recording_dispatcher):
    """重跑必须用新 task_id：沿用旧 id 会被 Arq 按 job_id 去重直接跳过。"""
    await ar.enqueue_auto_retry(user_id=7, article_id="art_1", url="https://x.com/a")

    (call,) = recording_dispatcher.calls
    assert call["task_id"].startswith("retry_")


# ---------------------------------------------------------------------------
# 缺陷 2：退款闸门
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("quota_charged", [True, False])
async def test_退款_只在_真扣过时发生(quota_charged, monkeypatch):
    """闸门本身：已扣 → 退；没扣 → 不退。

    直接把 `_refund_quota_once` 打成记录器（它是唯一退款入口），断言它有没有被调。
    两个方向都在参数里 —— 只修一个方向的话这条会红。
    """
    from tasks import distill_task as dt

    refunded: list[str] = []

    async def _recorder(task_id, user_id):
        refunded.append(task_id)

    async def _boom(*a, **k):
        raise RuntimeError("模拟 agent 失败")

    monkeypatch.setattr(dt, "_refund_quota_once", _recorder)
    monkeypatch.setattr(dt, "_run_distill_via_agent", _boom)
    monkeypatch.setattr(dt, "AsyncSessionLocal", _fake_session_factory())
    monkeypatch.setattr(dt, "_load_raw_content", _stub_raw_content())

    with pytest.raises(RuntimeError):
        await dt.distill_task(
            {"job_id": "j", "redis": None},
            "dst_1",
            "art_1",
            7,
            "https://x.com/a",
            quota_charged=quota_charged,
        )

    assert bool(refunded) is quota_charged, (
        f"quota_charged={quota_charged} 时退款调用应{'发生' if quota_charged else '不发生'}，"
        f"实际 refunded={refunded}"
    )


@pytest.mark.asyncio
async def test_默认就是_已扣配额(monkeypatch):
    """默认 True —— 剪藏正常路径（content-service 剪藏时就扣了）必须照常退款。

    这条专门防「把默认值写成 False」：那样所有正常剪藏的失败退款会全部消失，
    用户蒸馏失败一次白扣一次，且没有任何报错。是最容易犯、后果最重的改法。
    """
    import inspect

    from tasks import distill_task as dt

    sig = inspect.signature(dt.distill_task)
    assert sig.parameters["quota_charged"].default is True


@pytest.mark.asyncio
async def test_无正文失败时_未扣配额也不退(monkeypatch):
    """两个退款点都要守：这里测的是「无正文」那一条专用收口。

    无正文时任务不抛异常、直接 return，属另一条路径，容易只改一处。
    """
    from tasks import distill_task as dt

    refunded: list[str] = []

    async def _recorder(task_id, user_id):
        refunded.append(task_id)

    async def _empty(db, article_id):
        raise dt.EmptyArticleContentError("content_text 为空")

    monkeypatch.setattr(dt, "_refund_quota_once", _recorder)
    monkeypatch.setattr(dt, "_load_raw_content", _empty)
    monkeypatch.setattr(dt, "AsyncSessionLocal", _fake_session_factory())

    result = await dt.distill_task(
        {"job_id": "j", "redis": None},
        "dst_1",
        "art_1",
        7,
        "https://x.com/a",
        quota_charged=False,
    )

    assert result["reason"] == "empty_article_content"
    assert refunded == [], "没扣过配额却退了款 —— 用户白赚"


@pytest.mark.asyncio
async def test_无正文失败时_已扣配额照退(monkeypatch):
    """与上一条成对：这条路径上「已扣」的用户一样要能拿回钱。"""
    from tasks import distill_task as dt

    refunded: list[str] = []

    async def _recorder(task_id, user_id):
        refunded.append(task_id)

    async def _empty(db, article_id):
        raise dt.EmptyArticleContentError("content_text 为空")

    monkeypatch.setattr(dt, "_refund_quota_once", _recorder)
    monkeypatch.setattr(dt, "_load_raw_content", _empty)
    monkeypatch.setattr(dt, "AsyncSessionLocal", _fake_session_factory())

    await dt.distill_task({"job_id": "j", "redis": None}, "dst_1", "art_1", 7, "https://x.com/a")

    assert refunded == ["dst_1"], "已扣配额的用户失败后没拿到退款"


# ---------------------------------------------------------------------------
# 接线：每个入队调用方都要表态
# ---------------------------------------------------------------------------


def test_每个入队调用方_都显式表态_是否扣过配额():
    """源码级断言：`enqueue_distill(` 的每个调用点都写了 `quota_charged=`。

    靠默认值（True）会掩盖新调用方忘表态 —— 而「忘表态」的失败模式恰好是
    白退配额这种不报错的账。这里列出的三个调用方是全量，漏一个就红。
    """
    import ast
    from pathlib import Path

    ai_service = Path(__file__).resolve().parents[2] / "ai-service"
    callers = {
        "distill/auto_retry.py": False,  # 补偿重跑：没扣
        "main.py": None,  # 按 is_system_content 动态决定
    }

    for rel in callers:
        tree = ast.parse((ai_service / rel).read_text())
        found = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "enqueue_distill"
        ]
        assert found, f"{rel} 里没找到 enqueue_distill 调用 —— 调用方改名了请同步这条断言"

        for call in found:
            kwargs = {kw.arg for kw in call.keywords}
            assert "quota_charged" in kwargs, (
                f"{rel}:{call.lineno} 调用 enqueue_distill 没表态 quota_charged —— "
                "新调用方默认按「已扣」处理，退款闸门对它无效"
            )


def test_系统内容入队_声明_不扣配额():
    """uid==0（匿名剪藏 / 后台手动录入）跳过计费，入队必须同步表态。

    否则每次系统内容蒸馏失败都会去退 users.id=0 的额度（本就为 0），在
    quota_service._apply 里撞「used + delta < 0」抛 3003，被吞成一条
    quota_refund_failed —— 钱没白赚，但把「退款坏了」和「本来就没扣」两种故障
    混进同一条日志，排查时会往错的方向查。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "ai-service" / "main.py").read_text()

    assert (
        "quota_charged=not is_system_content" in src
    ), "main.py 的入队没按 is_system_content 表态，系统内容失败会刷误导性退款告警"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _fake_session_factory():
    from contextlib import asynccontextmanager

    class _Result:
        def scalar_one_or_none(self):
            return None

        def one_or_none(self):
            return None

        def scalars(self):
            return self

        def all(self):
            return []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt):
            return _Result()

        async def scalar(self, stmt):
            return "art_1"  # 文章存在，否则走「已删除」早退分支

        async def commit(self):
            return None

        async def flush(self):
            return None

        def add(self, obj):
            return None

        # 必须是**同步**方法返回 async CM：analytics.track 里写的是
        # `async with db.begin_nested()`。写成 `async def` 会返回一个协程，
        # `async with <coroutine>` 抛 AttributeError 被 analytics 的 except 吞成
        # 「埋点失败（忽略）」—— 测试照样绿，但埋点其实一次都没跑。
        def begin_nested(self):
            @asynccontextmanager
            async def _savepoint():
                yield self

            return _savepoint()

    @asynccontextmanager
    async def _local():
        yield _Session()

    return _local


def _stub_raw_content():
    async def _load(db, article_id):
        return f"[stub] {article_id}"

    return _load
