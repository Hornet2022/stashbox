"""剪藏失败必须当场报错，不得软降级进蒸馏（2026-10-03）。

**这个文件锁的是什么**

改造前 `_create_article` 里的抓取失败被 `except FetcherError` 整个吞掉，
文章照样建（`status=pending` + `raw_content=None`）、配额照扣、蒸馏照派，
接口返回 200。到了 ai-service，`_load_raw_content` 返回字面量
`"[empty article] title=… url=…"`，**这段占位符被当正文喂给了 LLM**。

一次网络抖动的完整代价：
    扣 1 次配额 → 建一条无正文的文章 → 派一个注定失败的蒸馏任务
    → 白烧 LLM token → 用户等 12 分钟看到失败

剪藏成功率是听匣的命门。这几条断言就是防止它退化回去的。

不依赖真实网络：全部通过 monkeypatch 替换 fetcher 工厂。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO_PARENT = str(CONTENT_SERVICE_DIR.parent.parent.parent)
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)


def _load_module(name: str, path: Path, submodule_search_locations=None):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=submodule_search_locations
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# tests/fetchers/ 也叫 `fetchers`，整目录跑时会把真包顶掉 —— 同 test_wechat_mp_handler.py
cs_fetchers = _load_module(
    "_capfail_fetchers_pkg",
    CONTENT_SERVICE_DIR / "fetchers" / "__init__.py",
    submodule_search_locations=[str(CONTENT_SERVICE_DIR / "fetchers")],
)
_shadowed = sys.modules.get("fetchers")
sys.modules["fetchers"] = cs_fetchers
try:
    content_main = _load_module("_capfail_content_main", CONTENT_SERVICE_DIR / "main.py")
finally:
    if _shadowed is None:
        del sys.modules["fetchers"]
    else:
        sys.modules["fetchers"] = _shadowed

FetcherError = cs_fetchers.FetcherError
FetcherErrorCode = cs_fetchers.FetcherErrorCode
FetchResult = cs_fetchers.FetchResult
BizException = content_main.BizException
_prefetch_for_capture = content_main._prefetch_for_capture


class _StubFetcher:
    """只会按预设抛错的 fetcher。"""

    def __init__(self, exc: Exception | None = None, result=None):
        self._exc = exc
        self._result = result
        self.calls = 0

    def supports(self, url: str) -> bool:
        return True

    async def fetch(self, url: str, *, timeout: float = 10.0):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


def _good_result() -> FetchResult:
    return FetchResult(
        url="https://example.com/a",
        title="真标题",
        content_html="<p>正文</p>",
        content_text="这是一段真实的正文，足够蒸馏。",
        author=None,
        publish_time=None,
        source="generic_url",
        media_urls=[],
    )


# ── 各错误码都必须抛出、且映射到对外契约 ──────────────────────────────
@pytest.mark.parametrize(
    ("fetcher_code", "expect_biz", "expect_status", "must_mention"),
    [
        (FetcherErrorCode.NETWORK, 2002, 502, "没能连上"),
        (FetcherErrorCode.RATE_LIMIT, 2002, 502, "限制"),
        (FetcherErrorCode.AUTH, 2002, 502, "微信"),
        (FetcherErrorCode.NOT_FOUND, 2002, 502, "删除"),
        (FetcherErrorCode.PARSE, 2002, 502, "正文"),
        (FetcherErrorCode.INTERNAL, 2002, 500, "出错"),
        (FetcherErrorCode.UNSUPPORTED, 2001, 400, "暂不支持"),
    ],
)
def test_prefetch_raises_on_every_failure_code(
    monkeypatch, fetcher_code, expect_biz, expect_status, must_mention
):
    """每一个 FetcherErrorCode 都必须冒泡成 BizException，不许软降级。"""
    stub = _StubFetcher(FetcherError(fetcher_code, "技术细节", source="wechat_mp"))
    monkeypatch.setattr(content_main, "get_fetcher", lambda url: stub)

    with pytest.raises(BizException) as ei:
        import asyncio

        asyncio.run(_prefetch_for_capture("https://mp.weixin.qq.com/s/xxx"))

    exc = ei.value
    assert exc.code == expect_biz
    assert exc.http_status == expect_status
    # 文案要可操作：说清谁的错、能不能重试、该做什么
    assert must_mention in exc.message
    # 内部代号不能泄给客户端
    assert "wechat_mp" not in exc.message
    assert "技术细节" not in exc.message


def test_prefetch_wraps_unexpected_exception_as_502(monkeypatch):
    """连 FetcherError 都没包的意外（超时/SSL）同样不能软降级。"""
    stub = _StubFetcher(TimeoutError("socket timed out"))
    monkeypatch.setattr(content_main, "get_fetcher", lambda url: stub)

    with pytest.raises(BizException) as ei:
        import asyncio

        asyncio.run(_prefetch_for_capture("https://example.com/a"))

    assert ei.value.code == 2002
    assert ei.value.http_status == 502
    assert "TimeoutError" not in ei.value.message  # 只进 detail
    assert "TimeoutError" in ei.value.detail


def test_prefetch_no_fetcher_is_2001_not_silent_skip(monkeypatch):
    """get_fetcher 返回 None 时原来直接 return，建出无正文文章 —— 同样必炸。"""
    monkeypatch.setattr(content_main, "get_fetcher", lambda url: None)

    with pytest.raises(BizException) as ei:
        import asyncio

        asyncio.run(_prefetch_for_capture("ftp://example.com/a"))

    assert ei.value.code == 2001
    assert ei.value.http_status == 400


def test_prefetch_success_returns_result(monkeypatch):
    """成功路径不受影响 —— 这是绝大多数剪藏，必须保持原样。"""
    stub = _StubFetcher(result=_good_result())
    monkeypatch.setattr(content_main, "get_fetcher", lambda url: stub)

    import asyncio

    fr = asyncio.run(_prefetch_for_capture("https://example.com/a"))
    assert fr.title == "真标题"
    assert fr.content_text


# ── 蒸馏侧：空正文绝不能进 LLM ────────────────────────────────────────
def test_load_raw_content_refuses_empty_content():
    """`[empty article]` 这种占位符一旦回到正文里，就是在烧 LLM 的钱。"""
    dt = _load_distill_task_module()

    class _Art:
        id = "art_x"
        title = "有标题没正文"
        url = "https://example.com/a"
        raw_content = None  # 抓取失败建出来的就是这种

    class _Db:
        async def scalar(self, *a, **k):
            return _Art()

    import asyncio

    with pytest.raises(dt.EmptyArticleContentError):
        asyncio.run(dt._load_raw_content(_Db(), "art_x"))


def test_load_raw_content_refuses_empty_content_text():
    dt = _load_distill_task_module()

    class _Art:
        id = "art_y"
        title = "标题"
        url = "https://example.com/a"
        raw_content = {"title": "标题", "content_text": "   "}  # 空白也算没有

    class _Db:
        async def scalar(self, *a, **k):
            return _Art()

    import asyncio

    with pytest.raises(dt.EmptyArticleContentError):
        asyncio.run(dt._load_raw_content(_Db(), "art_y"))


def test_load_raw_content_still_returns_real_content():
    dt = _load_distill_task_module()

    class _Art:
        id = "art_z"
        title = "标题"
        url = "https://example.com/a"
        raw_content = {"content_text": "这是真实正文。"}

    class _Db:
        async def scalar(self, *a, **k):
            return _Art()

    import asyncio

    assert asyncio.run(dt._load_raw_content(_Db(), "art_z")) == "这是真实正文。"


_dt_module = None


def _load_distill_task_module():
    """distill_task 在 ai-service 目录下，同样带连字符，只能按文件加载。

    它还会 `from llm import ...` / `from agent import ...` 拉同目录的兄弟模块，
    所以必须把 ai-service 目录本身加进 sys.path —— 光按文件加载会 ModuleNotFoundError。
    """
    global _dt_module
    if _dt_module is not None:
        return _dt_module
    backend_dir = CONTENT_SERVICE_DIR.parent
    ai_dir = backend_dir / "ai-service"
    for p in (str(ai_dir), str(backend_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)
    _dt_module = _load_module("_capfail_distill_task", ai_dir / "tasks" / "distill_task.py")
    return _dt_module
