"""GitHub 开源库抓取（2026-10-23）。

## 它解决什么

「剪藏一个 GitHub 仓库」此前会落进 `generic_url` 兜底 —— 而那个抽取器是
**网页密度启发式**（`parser.py`）：GitHub 仓库首页是个 JS 应用壳，
正文在 `/owner/repo` 之后异步加载 ⇒ 抽出来的是一堆导航文字。

⇒ 用户剪藏一个开源库，得到的是一篇**没有信息**的稿子。

这里给仓库一条专属通道：抓 **README 正文**（这是开源库真正的「文档」）
+ 仓库元信息（描述 / stars / 主语言 / license），拼成可蒸馏的正文。

## 为什么 README 走 raw.githubusercontent 而不是 GitHub API

`api.github.com` 的未认证限额是 **60 次/小时/IP** —— 而服务器是**共享出口
IP**（整台机器的用户共用一个配额）。几个内测用户各剪几个库就能把配额
打光，之后**所有人的 GitHub 剪藏全失败**，且报错是「rate limited」这种
用户完全看不懂的 403。

`raw.githubusercontent.com` 是 CDN，**没有这个配额** ⇒ README 走它是唯一
不会「用着用着突然全挂」的选择。

仓库元信息（stars / description / language）走 API，但**失败不阻塞**：
README 已经拿到了，稿子照做；元信息只是锦上添花。403/429 时只记 warning。

## 支持的 URL 形态

- `https://github.com/{owner}/{repo}`
- 带子路径的：`/tree/main`、`/blob/main/README.md`、`/releases`、`/issues` …
  —— 一律**归一到仓库根**（用户想剪的是这个库，不是它的某个文件）。
- `raw.githubusercontent.com/{owner}/{repo}/...` → 也归一到仓库根。
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import FetchResult, Fetcher, FetcherError, FetcherErrorCode
from .net import (
    HOST_THROTTLE,
    HeaderProfile,
    build_client_kwargs,
    fetch_with_retry,
)
from .tiers import host_of

log = logging.getLogger("stashbox.fetch.github")

SOURCE = "github"

#: README 的候选文件名。
#:
#: 为什么要试多个：GitHub 仓库页会**渲染**任意大小写/后缀的 README，
#: 但 `raw.githubusercontent.com` 是**按路径取文件**的 —— `Readme.md` 不会被
#: 自动归一化成 `README.md`。只试一两个名字 → 一半的仓库取不到。
#:
#: 覆盖三种常见写法：**全大写**（README.md）、**全小写**（readme.md）、
#: **首字母大写**（Readme.md —— 非常常见，很多项目就是这个名字）。
_README_CANDIDATES = (
    "README.md",
    "Readme.md",
    "readme.md",
    "README.MD",
    "README.markdown",
    "Readme.markdown",
    "readme.markdown",
    "README.rst",
    "Readme.rst",
    "readme.rst",
    "README.txt",
    "Readme.txt",
    "readme.txt",
    "README",
    "readme",
)

#: 仓库页 URL → (owner, repo)。
_REPO_PATH_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)")


def parse_repo_ref(url: str) -> tuple[str, str] | None:
    """从 URL 里解析 (owner, repo)；不是仓库链接则返回 None。

    归一化掉的形态（都指向同一个库）：
      · github.com/{owner}/{repo}
      · github.com/{owner}/{repo}/tree/main/src
      · raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    if host == "github.com":
        m = _REPO_PATH_RE.match(parsed.path)
    elif host == "raw.githubusercontent.com":
        # raw 的路径是 /{owner}/{repo}/{ref}/{path}
        m = _REPO_PATH_RE.match(parsed.path)
    elif host == "gist.github.com":
        m = None  # gist 不是仓库，不支持
    else:
        return None
    if not m:
        return None
    owner = m.group("owner")
    repo = m.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    # 排除 github.com 上的非仓库路径（/features /about /login /orgs/xxx …）
    if owner.lower() in {
        "features",
        "about",
        "login",
        "signup",
        "orgs",
        "settings",
        "pricing",
        "sponsors",
    }:
        return None
    if not owner or not repo:
        return None
    return owner, repo


class GitHubFetcher(Fetcher):
    """GitHub 仓库抓取器：README 正文 + 仓库元信息。"""

    PROFILE = HeaderProfile.DESKTOP  # GitHub 对桌面 UA 最友好
    TIMEOUT = 30.0

    @property
    def name(self) -> str:
        return SOURCE

    def supports(self, url: str) -> bool:
        return parse_repo_ref(url) is not None

    async def fetch(
        self, url: str, *, timeout: float = TIMEOUT, budget: float | None = None
    ) -> FetchResult:
        ref = parse_repo_ref(url)
        if ref is None:
            raise FetcherError(
                code=FetcherErrorCode.UNSUPPORTED,
                message=f"not a github repo url: {url!r}",
                source=self.name,
            )
        owner, repo = ref
        repo_url = f"https://github.com/{owner}/{repo}"

        client_kwargs = build_client_kwargs(self.PROFILE, guard_ssrf=True)
        client = httpx.AsyncClient(**client_kwargs)

        try:
            readme = await self._fetch_readme(client, owner, repo, timeout=timeout)
            meta = await self._fetch_repo_meta(client, owner, repo, timeout=timeout)
        finally:
            await client.aclose()
        # 正文 = README + 元信息摘要。
        #
        # 为什么把元信息也拼进正文：改写的 LLM 只看正文（`fetched_content`），
        # 而「这是个 12k star 的 Python 库」这类信息**只在元信息里**。
        # 不拼进去，稿子就不知道这是个什么规模的库 —— 关联思考（related：
        # 和什么项目可比）也就没有依据。
        header = _meta_header(owner, repo, meta)
        content_text = f"{header}\n\n{readme}".strip()

        if len(content_text.strip()) < 200:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"github repo content too short: {len(content_text.strip())} chars",
                source=self.name,
            )

        raw_metadata: dict[str, Any] = {
            "owner": owner,
            "repo": repo,
            "repo_url": repo_url,
            "readme_chars": len(readme),
            **(meta or {}),
        }

        return FetchResult(
            url=url,
            title=meta.get("full_name") if meta and meta.get("full_name") else f"{owner}/{repo}",
            content_html=f"<pre>{readme}</pre>",
            content_text=content_text,
            author=meta.get("owner_login") if meta else None,
            publish_time=None,
            media_urls=[],
            source=self.name,
            raw_metadata=raw_metadata,
        )

    async def _fetch_readme(
        self, client: httpx.AsyncClient, owner: str, repo: str, *, timeout: float
    ) -> str:
        """抓 README。**两条通道，raw 优先、API 兜底**。

        ## 为什么必须有两条通道（2026-10-23 生产实测）

        在**国内服务器**上实测：
        · `raw.githubusercontent.com` → **连不上**（10s 超时，http=000）
        · `api.github.com` → **通**（0.6s，200）

        而在本地/海外机器上正好相反（raw 通、api 慢）。只写一条通道 =
        一半的部署环境完全用不了这个功能。

        ## 通道的取舍

        · **raw**（`raw.githubusercontent.com/{owner}/{repo}/HEAD/{name}`）
          —— 快、**没有速率限制**，但国内不通。
        · **API**（`api.github.com/repos/{owner}/{repo}/readme`）
          —— 国内可达，但**未认证限额 60 次/小时/IP**，而服务器是共享出口 IP。

        ⇒ 顺序是「raw 优先（多数环境走它）→ raw 不通再走 API（生产走它）」。
        API 的额度只在 raw 失败时才消耗，够内测阶段用。

        ## 为什么走 `fetch_with_retry`

        `raw.githubusercontent.com` 会**偶发超时**（实测 psf/requests 一次
        ReadTimeout，同 URL 重试就好）。直接 `client.get()` 的话，一次网络
        抖动 = 整个剪藏失败 —— 而其它 fetcher 都走了重试，只有这里没走，
        就是**同一个坑只踩一次**。
        """
        await HOST_THROTTLE.wait(host_of("github.com"))
        raw_error = ""
        try:
            return await self._fetch_readme_via_raw(client, owner, repo, timeout=timeout)
        except FetcherError as exc:
            if exc.code is not FetcherErrorCode.NOT_FOUND:
                # 不是「仓库没有 README」，而是 raw 这条路**网络不通**
                # （国内服务器就是如此）→ 换 API 通道再试一次。
                raw_error = exc.message
                log.info(
                    "github_raw_channel_failed owner=%s repo=%s err=%s", owner, repo, exc.message
                )
            else:
                # 仓库确实没有 README → API 通道也一样没有，直接报
                raise

        # ── 通道二：GitHub API（国内可达，但有 60 次/小时的共享 IP 限额）──
        text = await self._fetch_readme_via_api(client, owner, repo, timeout=timeout)
        if text:
            return text
        raise FetcherError(
            code=FetcherErrorCode.NOT_FOUND,
            message=(f"github repo has no readable README " f"(raw 通道: {raw_error or '404'})"),
            source=self.name,
        )

    async def _fetch_readme_via_raw(
        self, client: httpx.AsyncClient, owner: str, repo: str, *, timeout: float
    ) -> str:
        """通道一：raw.githubusercontent（快、无限额；国内常不通）。"""
        last_status = 0
        for candidate in _README_CANDIDATES:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{candidate}"

            async def send(per_attempt: float, _u: str = raw_url) -> httpx.Response:
                return await client.get(_u, timeout=per_attempt)

            try:
                resp = await fetch_with_retry(
                    send, on_retry=lambda n, why: log.info("github_retry attempt=%d %s", n, why)
                )
            except httpx.HTTPError as exc:
                # 连不上（国内服务器）→ 交给上层换通道
                raise FetcherError(
                    code=FetcherErrorCode.NETWORK,
                    message=f"raw 通道不可达: {type(exc).__name__}",
                    source=self.name,
                ) from exc
            last_status = resp.status_code
            if resp.status_code == 200:
                text = resp.text.strip()
                if text:
                    return text
            if resp.status_code not in (404, 403):
                # 其它错误码（5xx / 429）→ 这次就别试下一个文件名了
                raise FetcherError(
                    code=FetcherErrorCode.NETWORK,
                    message=f"github readme http {resp.status_code}",
                    source=self.name,
                )
        raise FetcherError(
            code=FetcherErrorCode.NOT_FOUND,
            message=f"github repo has no readable README (last status {last_status})",
            source=self.name,
        )

    async def _fetch_readme_via_api(
        self, client: httpx.AsyncClient, owner: str, repo: str, *, timeout: float
    ) -> str:
        """通道二：GitHub API 的 `/readme`（国内可达；60 次/小时/共享 IP）。"""
        api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        try:
            resp = await client.get(
                api_url,
                timeout=timeout,
                headers={"Accept": "application/vnd.github+json"},
            )
        except httpx.HTTPError as exc:
            raise FetcherError(
                code=FetcherErrorCode.NETWORK,
                message=f"github api 通道不可达: {type(exc).__name__}",
                source=self.name,
            ) from exc
        if resp.status_code != 200:
            # 403 = 速率限制耗尽（共享 IP 的常见结局），404 = 仓库没有 README
            code = (
                FetcherErrorCode.RATE_LIMIT
                if resp.status_code == 403
                else FetcherErrorCode.NOT_FOUND
            )
            raise FetcherError(
                code=code,
                message=f"github readme api http {resp.status_code}",
                source=self.name,
            )
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"github readme api json failed: {exc}",
                source=self.name,
            ) from exc
        if not isinstance(data, dict):
            return ""
        # README > 1MB 时 API 不返回 content（要再走 blob 端点）—— 那种 README
        # 对播客化改写本来也没用（远超 20 万字的正文上限），直接放弃。
        encoding = str(data.get("encoding") or "")
        content = str(data.get("content") or "")
        if encoding == "base64" and content:
            import base64

            try:
                return base64.b64decode(content).decode("utf-8", errors="replace").strip()
            except Exception as exc:  # noqa: BLE001
                log.warning("github_readme_b64_failed owner=%s repo=%s err=%s", owner, repo, exc)
                return ""
        if encoding == "none":
            log.info(
                "github_readme_too_large_for_api owner=%s repo=%s size=%s",
                owner,
                repo,
                data.get("size"),
            )
        return ""

    async def _fetch_repo_meta(
        self, client: httpx.AsyncClient, owner: str, repo: str, *, timeout: float
    ) -> dict[str, Any]:
        """仓库元信息（stars / 描述 / 语言 / license）。

        **失败不阻塞**：README 已经拿到了，稿子照做。
        共享出口 IP 的未认证 API 配额只有 60 次/小时，很容易被打光 ——
        那时只该少几个字段，不该让整个剪藏失败。
        """
        api_url = f"https://api.github.com/repos/{owner}/{repo}"
        try:
            resp = await client.get(
                api_url,
                timeout=timeout,
                headers={"Accept": "application/vnd.github+json"},
            )
        except httpx.HTTPError as exc:
            log.warning("github_meta_fetch_failed owner=%s repo=%s err=%s", owner, repo, exc)
            return {}
        if resp.status_code != 200:
            # 403 = 速率限制（共享 IP 的常见结局），404 = 仓库不存在/改名
            log.warning(
                "github_meta_http_status owner=%s repo=%s status=%s",
                owner,
                repo,
                resp.status_code,
            )
            return {}
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("github_meta_json_failed owner=%s repo=%s err=%s", owner, repo, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        meta: dict[str, Any] = {}
        for key in (
            "full_name",
            "description",
            "stargazers_count",
            "forks_count",
            "open_issues_count",
            "language",
            "topics",
            "default_branch",
            "pushed_at",
            "created_at",
        ):
            if key in data:
                meta[key] = data[key]
        license_info = data.get("license")
        if isinstance(license_info, dict) and license_info.get("spdx_id"):
            meta["license"] = license_info["spdx_id"]
        return meta


def _meta_header(owner: str, repo: str, meta: dict[str, Any] | None) -> str:
    """仓库元信息 → 拼在 README 前面的一段事实描述。

    只放**事实**（数字、名称、语言），不放评价 —— 评价是 LLM 的活，
    我们只保证它有依据。
    """
    meta = meta or {}
    bits: list[str] = [f"GitHub 仓库：{owner}/{repo}（https://github.com/{owner}/{repo}）"]
    if meta.get("description"):
        bits.append(f"项目描述：{meta['description']}")
    if meta.get("stargazers_count") is not None:
        bits.append(f"Star 数：{meta['stargazers_count']}")
    if meta.get("forks_count") is not None:
        bits.append(f"Fork 数：{meta['forks_count']}")
    if meta.get("language"):
        bits.append(f"主要语言：{meta['language']}")
    if meta.get("license"):
        bits.append(f"许可证：{meta['license']}")
    if meta.get("topics"):
        topics = meta["topics"]
        if isinstance(topics, list) and topics:
            bits.append("主题标签：" + "、".join(str(t) for t in topics[:10]))
    return "\n".join(bits)


__all__ = ["GitHubFetcher", "SOURCE", "parse_repo_ref"]
