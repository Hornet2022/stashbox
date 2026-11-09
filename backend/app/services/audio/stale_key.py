"""过期音频 key 的识别与重定向（2026-11-09）。

## 背景：内容寻址的 key 有一个「客户端缓存旧 URL」的代价

音频 key 是**内容寻址**的：`audio/{article_id}.{hash8}.{ext}`
（见 `app/services/storage/base.py::versioned_audio_key`）。
这是为了解决 2026-10-21 的事故：同一篇文章重新蒸馏后，
URL 不变 ⇒ 安卓 Media3 的 SimpleCache（按 URL 做 key，且**永不重新验证**）
会一直播旧音频 ⇒ 用户反馈「稿子是新的、语音是旧的」。

代价是：**重新生成音频 ⇒ key 变 ⇒ 旧文件被删**
（`ai-service/tasks/distill_task.py::_delete_superseded_audio`）⇒
**客户端手里缓存的旧 URL 变成 404**。

## 实测（2026-11-09 内测）

18:46 我做端到端验证时重跑了 `art_3dd88aca…` 的蒸馏 ⇒
音频重新生成（key `4c67fadb` → `50967cd2`）⇒ 旧文件被删。

19:27 起该用户的客户端一直用缓存里的旧 URL 播放 ⇒ 服务端 **404** ⇒
客户端把它归类成「瞬时网络类」⇒ 界面显示「**网络不稳，正在重连…**」，
并在同一个永远不存在的文件上反复自动重连。

⇒ 用户的原话「手机网络正常」是**完全正确**的：网络没问题，
是服务端说「这个文件没了」，而客户端把这件事翻译成了「网络不稳」。

## 治本（服务端侧，已装用户立刻自愈，不需要发版）

请求的文件不存在时：

    查该文章**当前**的音频 key ⇒ 若与请求的不同 ⇒ **302 重定向**过去。

这样旧 URL 永远拿得到**当前**音频：

- 不会 404（用户不再看到「网络不稳」）；
- 也不会播到旧音频（重定向指向的是当前版本）；
- 已装的旧版本客户端**不需要更新**就能恢复。

判据（为什么不查「最近一次」而是「当前」）：
唯一真相源是 DB 里 `distilled_articles.audio_url` ——
磁盘上的文件可以缺（被删、被清），DB 里的当前 key 才是事实。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 音频 key：audio/{article_id}.{hash}.{ext}
# article_id 形如 art_3dd88aca36f645c6aee0a5be（字母数字下划线）
# hash 是内容 sha256 的前若干位（十六进制）
# ext 是扩展名（mp3/m4a/wav…）
AUDIO_KEY_RE = re.compile(
    r"^audio/(?P<article_id>[A-Za-z0-9_-]+)\.(?P<hash>[0-9a-f]{4,64})\.(?P<ext>[A-Za-z0-9]+)$"
)

# 挂载点前缀：URL 形如 /audio/audio/xxx.mp3 ⇒ 挂载内路径是 audio/xxx.mp3
MOUNT_PREFIX = "/audio"


@dataclass(frozen=True)
class AudioKey:
    """一个音频 key 的结构化形式。"""

    article_id: str
    content_hash: str
    ext: str

    @property
    def key(self) -> str:
        """存储层用的 key（相对 LOCAL_AUDIO_DIR / OSS 根）。"""
        return f"audio/{self.article_id}.{self.content_hash}.{self.ext}"

    @property
    def public_path(self) -> str:
        """对外 URL 的 path 部分。"""
        return f"{MOUNT_PREFIX}/{self.key}"


def parse_audio_key(path: str) -> AudioKey | None:
    """把挂载内路径（`audio/xxx.mp3`）解析成 [AudioKey]；不像音频 key 则返回 None。

    为什么要"不像就返回 None"而不是抛异常：网关要处理的是**任意路径**
    （静态目录里还有别的东西），只有"长得像带 hash 的音频 key"才需要查库。
    """
    if not path:
        return None
    normalized = path.strip().lstrip("/")
    m = AUDIO_KEY_RE.match(normalized)
    if not m:
        return None
    return AudioKey(
        article_id=m.group("article_id"),
        content_hash=m.group("hash"),
        ext=m.group("ext"),
    )


def key_from_audio_url(url: str | None) -> str | None:
    """从完整 URL 里取存储 key（`https://host/audio/audio/x.mp3` → `audio/x.mp3`）。

    取不到就返回 None —— 调用方要能区分"没有 URL"和"URL 格式不认识"，
    前者说明这篇文章压根没音频。
    """
    if not url:
        return None
    # 去掉 query / fragment
    cleaned = url.split("?", 1)[0].split("#", 1)[0]
    # 取最后一段路径
    tail = cleaned.rstrip("/").rsplit("/", 1)[-1]
    if not tail:
        return None
    candidate = f"audio/{tail}"
    return candidate if AUDIO_KEY_RE.match(candidate) else None


def is_stale_request(requested_key: str, current_key: str | None) -> bool:
    """请求的 key 是否是"已被替换掉的旧版本"。

    · 没有当前 key（文章没音频了 / 已被删）⇒ 不是 stale（该 404 就 404）；
    · 当前 key 与请求的相同 ⇒ 不是 stale（文件真的不存在，是别的问题）；
    · 不然 ⇒ stale（该重定向到当前 key）。
    """
    if not current_key:
        return False
    return current_key != requested_key
