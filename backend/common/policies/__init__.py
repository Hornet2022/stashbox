"""隐私政策正文加载。

为什么正文放服务端而不是安卓资源里
----------------------------------
- **版本和正文不会走散**：客户端显示"你同意的是 v1"时，服务端给的就是 v1 的正文。
  两边各存一份迟早会出现"客户端说 v1、服务端已经 v2"的错配。
- **政策改版不用发版**：改正文 + 改版本号，客户端下次启动拉最新的就行
  （前提是客户端每次启动都拉 —— 见 GET /api/v1/policies/privacy）。

文件头用 HTML 注释带元信息（version / updated），改动时记得同步：
**改版本号 = 要求所有用户重新同意**，客户端会比对版本号。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_POLICY_DIR = Path(__file__).resolve().parent
_PRIVACY_FILE = _POLICY_DIR / "privacy_policy.md"

#: 文件头用 HTML 注释带元信息（version / updated）。
#:
#: 注意匹配的是**所有** HTML 注释行，而不只是 version/updated 那两条 ——
#: 文件第一行还写着 `<!-- 听匣隐私政策 -->`（当初是当标题用的），
#: 只剥 version/updated 的话，这一行会**原样显示在政策正文第一行**。
#: 2026-10-21 真机 E2E 实测到的：用户看到的是
#:   <!-- 听匣隐私政策 -->
#   听匣隐私政策
#   版本：v1　最后更新：2026-10-21
#: —— 第一行是原始 HTML 注释。
_META_RE = re.compile(r"^<!--\s*(version|updated):\s*(.+?)\s*-->$", re.M)

#: **所有** HTML 注释行。
#:
#: 不能只剥 version/updated 那两条 —— 文件第一行还写着
#: `<!-- 听匣隐私政策 -->`（当初是当标题用的），只剥两条的话，
#: 这一行会**原样显示在政策正文第一行**。
#: 2026-10-21 真机 E2E 实测到的：用户看到的是
#:   <!-- 听匣隐私政策 -->
#:   听匣隐私政策
#:   版本：v1　最后更新：2026-10-21
#: —— 第一行是原始 HTML 注释。
_COMMENT_RE = re.compile(r"^<!--.*?-->\s*$", re.M)


class PolicyNotFound(RuntimeError):
    """政策文件不存在或读不出来。"""


def _read_meta(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in _META_RE.finditer(text)}


def _strip_meta(text: str) -> str:
    """把文件头的 HTML 注释（**所有**）去掉，只留正文。

    只剥 version/updated 是不够的 —— 见 [_COMMENT_RE] 的说明。
    """
    return _COMMENT_RE.sub("", text).lstrip("\n")


@lru_cache(maxsize=4)
def load_privacy_policy() -> dict[str, str]:
    """读隐私政策，返回 {version, updated_at, title, content}。

    做了 lru_cache：政策正文是静态文件，没必要每个请求都读盘。
    改了文件重启服务即可生效（内测阶段够用；真要热更再引入文件监听）。
    """
    if not _PRIVACY_FILE.exists():
        raise PolicyNotFound(f"隐私政策文件不存在：{_PRIVACY_FILE}")
    try:
        text = _PRIVACY_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyNotFound(f"隐私政策文件读取失败：{_PRIVACY_FILE} ({exc})") from exc

    meta = _read_meta(text)
    body = _strip_meta(text)
    title = ""
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    return {
        "version": meta.get("version", "v1"),
        "updated_at": meta.get("updated", ""),
        "title": title or "隐私政策",
        "content": body,
    }


def privacy_policy_version() -> str:
    """当前政策版本号（客户端比对用）。"""
    return load_privacy_policy()["version"]
