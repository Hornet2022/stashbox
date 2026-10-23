"""GitHub 仓库抓取（2026-10-23）。

## 为什么要有这些用例

GitHub 仓库此前会落进 `generic_url` 兜底 —— 而那个抽取器是**网页密度启发式**：
GitHub 仓库首页是 JS 应用壳，正文异步加载 ⇒ 抽出来的是一堆导航文字。
用户剪藏一个开源库，拿到一篇**没有信息**的稿子。

这里锁三件事：

1. **URL 归一化**：`/tree/main`、`/blob/main/x.py`、`raw.githubusercontent.com/...`
   都要归到同一个仓库；`github.com/features` 这类非仓库路径不能误判。
2. **注册顺序**：`GitHubFetcher` 必须在 `GenericURLFetcher`（catch-all）**之前** ——
   放后面就永远轮不到。这条最容易在「新增 fetcher」时被忽略。
3. **README 走 raw 而非 API**：`api.github.com` 未认证限额 60 次/小时/**IP**，
   而服务器是共享出口 IP ⇒ 几个用户各剪几个库就能把配额打光，
   之后**所有人的 GitHub 剪藏全失败**。所以 README 必须走 raw CDN。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
CONTENT_DIR = BACKEND_DIR / "content-service"
if str(CONTENT_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_DIR))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        CONTENT_DIR / "fetchers" / "__init__.py",
        submodule_search_locations=[str(CONTENT_DIR / "fetchers")],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("cs_gh_fetchers")
gh = importlib.import_module("cs_gh_fetchers.github")
fetchers_mod = sys.modules["cs_gh_fetchers"]


# ---------------------------------------------------------------------------
# URL 归一化
# ---------------------------------------------------------------------------
def _patch_transport(monkeypatch, transport) -> None:
    """让 fetcher 建出来的 client 用假传输层。

    ⚠️ **不能** monkeypatch `httpx.AsyncClient` 本身 —— `gh.httpx` 就是
    `httpx` 模块本身，patch 掉它的类之后，我们在 lambda 里再调
    `httpx.AsyncClient(...)` 会**再次命中被 patch 的版本** → 无限递归。

    正解：patch `build_client_kwargs`（fetcher 建 client 的唯一入口），
    把 transport 塞进它的返回值。
    """
    real = gh.build_client_kwargs

    def _wrapped(*args, **kwargs):
        kwargs.pop("transport", None)
        out = real(*args, **kwargs)
        out["transport"] = transport
        return out

    monkeypatch.setattr(gh, "build_client_kwargs", _wrapped)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/psf/requests", ("psf", "requests")),
        ("https://github.com/psf/requests/tree/main/src", ("psf", "requests")),
        ("https://github.com/psf/requests/blob/main/README.md", ("psf", "requests")),
        ("https://github.com/psf/requests/releases", ("psf", "requests")),
        ("https://github.com/psf/requests.git", ("psf", "requests")),
        ("https://github.com/psf/requests/", ("psf", "requests")),
        ("https://www.github.com/psf/requests", ("psf", "requests")),
        ("https://raw.githubusercontent.com/psf/requests/HEAD/README.md", ("psf", "requests")),
    ],
)
def test_仓库_url_归一化(url, expected):
    assert gh.parse_repo_ref(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/features",  # GitHub 的功能页，不是仓库
        "https://github.com/psf",  # 只有 owner
        "https://github.com/",  # 空路径
        "https://example.com/x",  # 别的站
        "https://gist.github.com/user/abc",  # gist 不是仓库
    ],
)
def test_非仓库_url_不认(url):
    assert gh.parse_repo_ref(url) is None


def test_supports_只对仓库_url_为真():
    f = gh.GitHubFetcher()
    assert f.supports("https://github.com/psf/requests")
    assert not f.supports("https://github.com/features")
    assert not f.supports("https://example.com/x")


# ---------------------------------------------------------------------------
# 注册顺序（最容易被忽略的一条）
# ---------------------------------------------------------------------------
def test_github_fetcher_排在_generic_之前():
    """catch-all 必须垫底 —— 放在它前面就永远轮不到。"""
    names = [type(f).__name__ for f in fetchers_mod._ALL_FETCHERS]
    assert names.index("GitHubFetcher") < names.index("GenericURLFetcher")


def test_get_fetcher_对_github_url_选_github():
    f = fetchers_mod.get_fetcher("https://github.com/psf/requests")
    assert isinstance(f, gh.GitHubFetcher)


# ---------------------------------------------------------------------------
# README 抓取（用 MockTransport，不出网）
# ---------------------------------------------------------------------------
#: README 正文必须够长（服务端对 <200 字符判「正文过短」——
#: 太短的 README 确实没法做播客化分析）。
_LONG_README = "# 标题\n\n" + ("这是一段足够长的项目说明文字，用来通过正文长度下限。" * 20)


def _readme_transport(readme_status: int = 200, readme_body: str = _LONG_README):
    """构造一个假的传输层：raw.githubusercontent 有 README、api.github.com 有元信息。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "raw.githubusercontent.com" in url:
            if readme_status != 200:
                return httpx.Response(readme_status, text="404: Not Found")
            return httpx.Response(200, text=readme_body)
        if "api.github.com" in url:
            return httpx.Response(
                200,
                json={
                    "full_name": "psf/requests",
                    "description": "A simple HTTP library",
                    "stargazers_count": 54000,
                    "forks_count": 9000,
                    "language": "Python",
                    "license": {"spdx_id": "Apache-2.0"},
                    "topics": ["http", "python"],
                },
            )
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_抓取_产出_readme_与元信息(monkeypatch):
    f = gh.GitHubFetcher()
    transport = _readme_transport()
    _patch_transport(monkeypatch, transport)

    result = await f.fetch("https://github.com/psf/requests")
    assert result.source == "github"
    assert "正文" in result.content_text
    # 元信息被拼进正文 —— 改写 LLM 只看正文，不拼它就不知道这是个什么库
    assert "Star 数：54000" in result.content_text
    assert "主要语言：Python" in result.content_text
    assert result.raw_metadata["owner"] == "psf"
    assert result.raw_metadata["repo"] == "requests"
    assert result.title == "psf/requests"


@pytest.mark.asyncio
async def test_元信息拿不到_也不影响_正文(monkeypatch):
    """API 403（共享 IP 的速率限制是常见结局）→ README 仍要拿到，稿子照做。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "raw.githubusercontent.com" in str(request.url):
            return httpx.Response(200, text="# 标题\n\n" + "正文内容。" * 60)
        return httpx.Response(403, text="rate limit exceeded")

    f = gh.GitHubFetcher()
    _patch_transport(monkeypatch, httpx.MockTransport(handler))
    result = await f.fetch("https://github.com/psf/requests")
    assert "正文内容" in result.content_text
    assert "Star 数" not in result.content_text


@pytest.mark.asyncio
async def test_没有_readme_报_not_found(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="404: Not Found")

    f = gh.GitHubFetcher()
    _patch_transport(monkeypatch, httpx.MockTransport(handler))
    from cs_gh_fetchers.base import FetcherError

    with pytest.raises(FetcherError) as exc:
        await f.fetch("https://github.com/psf/requests")
    assert exc.value.code.value == "fetcher.not_found"


@pytest.mark.asyncio
async def test_大写变体的_readme_也能取到(monkeypatch):
    """GitHub 仓库页会渲染任意大小写的 README，但 raw 是**按路径取文件**的。

    只试 `README.md` → 一半仓库取不到。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "Readme.md" in str(request.url):
            return httpx.Response(200, text="# 标题\n\n" + "内容。" * 60)
        return httpx.Response(404, text="404: Not Found")

    f = gh.GitHubFetcher()
    _patch_transport(monkeypatch, httpx.MockTransport(handler))
    result = await f.fetch("https://github.com/psf/requests")
    assert "内容" in result.content_text
