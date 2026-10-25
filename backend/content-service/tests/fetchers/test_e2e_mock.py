"""e2e mock 抓取器的契约（2026-10-25）。

## 这个文件守的是什么

`fetchers/e2e_mock.py` 让 e2e 栈在**没有外网**的情况下也能真跑「剪藏 → 建文章」
这条链路（2026-10-03 起入口改成「先抓后扣」，夹具 URL 必须真能抓到，
而 e2e 栈在本机、抓取器有 SSRF 防护 ⇒ 只能由这份夹具页顶上）。

它同时是**安全边界**：mock 模式下一个真实请求都不该发出去。
所以这里除了验「夹具能被解析」，还要验「**关掉开关时行为逐字节不变**」。

## 为什么用 importlib 重新加载

fetcher 列表在 `fetchers/__init__.py` 的 **import 期**按开关装配 ⇒
每个用例都要在**设好环境变量之后**重新加载被测包，才能测到对应的那套装配。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import httpx
import pytest

CONTENT_SERVICE_DIR = Path(__file__).resolve().parents[2]  # backend/content-service/
FETCHERS_DIR = CONTENT_SERVICE_DIR / "fetchers"


def _load_fetchers(env: dict[str, str]):
    """在给定环境变量下**重新**加载 fetchers 包，返回 (包, 加载后残留的 env)。"""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        name = "cs_fetchers_e2emock"
        for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[mod]
        spec = importlib.util.spec_from_file_location(
            name,
            FETCHERS_DIR / "__init__.py",
            submodule_search_locations=[str(FETCHERS_DIR)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _mock_module():
    """单独加载 e2e_mock 模块（不经包，避免被上面那个别名污染）。"""
    name = "cs_e2e_mock_under_test"
    for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        del sys.modules[mod]
    spec = importlib.util.spec_from_file_location(name, FETCHERS_DIR / "e2e_mock.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# ① 开关关着 = 生产路径不变
# ---------------------------------------------------------------------------


def test_开关关着_装全部五个fetcher():
    """生产路径必须**逐字节不变**：少装一个都是行为变更。"""
    cs = _load_fetchers({"STASHBOX_E2E_FETCH_MOCK": "", "ENVIRONMENT": "prod"})
    assert len(cs._ALL_FETCHERS) == 5
    names = [f.name for f in cs._ALL_FETCHERS]
    assert names[0] == "wechat_mp"
    assert names[-1] == "generic_url"


def test_开关关着_不产生mock_transport():
    mock = _mock_module()
    os.environ.pop("STASHBOX_E2E_FETCH_MOCK", None)
    assert mock.build_transport() is None


# ---------------------------------------------------------------------------
# ② 开关开着 = 只装两个、且不可能发出真实请求
# ---------------------------------------------------------------------------


def test_开关开着_只装两个fetcher():
    """douyin/pdf/github 没有 transport 注入点 ⇒ 装了就会发真实请求。

    所以 mock 模式**必须**只装 wechat + generic：少装换来一条硬保证 ——
    **mock 模式下不可能有任何真实请求发出去**。
    """
    cs = _load_fetchers({"STASHBOX_E2E_FETCH_MOCK": "1", "ENVIRONMENT": "dev"})
    assert len(cs._ALL_FETCHERS) == 2
    assert [f.name for f in cs._ALL_FETCHERS] == ["wechat_mp", "generic_url"]


@pytest.mark.asyncio
async def test_mock模式_连github域名也返回夹具():
    """**关键性质**：连 GitHub 这种专属域名也返回夹具 ⇒ 不可能走真实抓取。

    如果哪天有人把 GitHubFetcher 加回 mock 模式的列表，这条会立刻红
    （它没有 transport 注入点 ⇒ 会真的去访问 github.com）。
    """
    cs = _load_fetchers({"STASHBOX_E2E_FETCH_MOCK": "1", "ENVIRONMENT": "dev"})
    fetcher = cs.get_fetcher("https://github.com/some/repo")
    assert fetcher is not None
    result = await fetcher.fetch("https://github.com/some/repo")
    assert "e2e 夹具文章" in (result.title or "")


# ---------------------------------------------------------------------------
# ③ 夹具页本身要能被 wechat 解析器吃下
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock夹具_wechat能解析出标题与正文():
    """夹具页必须满足 wechat 解析器的全部硬要求，否则 e2e 照样过不去。"""
    cs = _load_fetchers({"STASHBOX_E2E_FETCH_MOCK": "1", "ENVIRONMENT": "dev"})
    fetcher = cs.get_fetcher("https://mp.weixin.qq.com/s/e2e_marker_123")
    assert fetcher is not None
    result = await fetcher.fetch("https://mp.weixin.qq.com/s/e2e_marker_123")
    assert result.title and "e2e_marker_123" in result.title
    assert result.content_text and "夹具" in result.content_text
    # 正文长度要过 wechat 的「合理性闸门」（MIN_ARTICLE_CHARS = 200）
    assert len(result.content_text) >= 200


def test_夹具正文长度够过闸门():
    """闸门常量在 parser.py；夹具正文必须明显高于它。"""
    mock = _mock_module()
    body = mock._TEMPLATE.format(title="t", marker="m", min_chars=mock._BODY_CHARS)
    # 粗略量：把标签剥掉之后的可见文本长度
    import re

    text = re.sub(r"<[^>]+>", "", body)
    assert len(text) > 200


# ---------------------------------------------------------------------------
# ④ 生产环境误设开关 ⇒ 直接炸
# ---------------------------------------------------------------------------


def test_生产环境设开关_抛错():
    """生产上返回假正文比返回错误严重得多 ⇒ 宁可进程起不来。"""
    mock = _mock_module()
    os.environ["STASHBOX_E2E_FETCH_MOCK"] = "1"
    os.environ["ENVIRONMENT"] = "prod"
    try:
        with pytest.raises(RuntimeError, match="只允许在非生产环境"):
            mock.build_transport()
    finally:
        os.environ.pop("STASHBOX_E2E_FETCH_MOCK", None)
        os.environ["ENVIRONMENT"] = "dev"


# ---------------------------------------------------------------------------
# ⑤ mock transport 本身不发真实请求
# ---------------------------------------------------------------------------


def test_mock_transport是内存里的():
    """用 httpx.MockTransport ⇒ 请求根本没出网。"""
    mock = _mock_module()
    os.environ["STASHBOX_E2E_FETCH_MOCK"] = "1"
    os.environ["ENVIRONMENT"] = "dev"
    try:
        transport = mock.build_transport()
        assert isinstance(transport, httpx.MockTransport)
    finally:
        os.environ.pop("STASHBOX_E2E_FETCH_MOCK", None)
