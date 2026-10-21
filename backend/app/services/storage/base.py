"""Storage 抽象接口。"""

import hashlib
from abc import ABC, abstractmethod


def content_hash(data: bytes, length: int = 8) -> str:
    """内容 hash（sha256 前 length 位十六进制）。

    用于给音频 key 加内容版本 —— 理由见 versioned_audio_key。
    """
    return hashlib.sha256(data).hexdigest()[:length]


def versioned_audio_key(article_id: str, ext: str, data: bytes) -> str:
    """内容寻址的音频 key：`audio/{article_id}.{hash8}.{ext}`。

    ## 为什么 key 必须带内容 hash

    客户端（安卓 Media3 SimpleCache）**按 URL 做缓存 key**，而且
    SimpleCache 对已缓存的 URL **永不向服务器重新验证** —— 缓存里有什么就放什么。

    而同一篇文章的音频是可以**被重新生成**的：
    换音色重新生成、蒸馏重跑、重新蒸馏、失败后重试。
    如果 URL 永远不变（`audio/{article_id}.{ext}`），客户端就会一直播**旧文件**：
    稿子（script_text，从 API 拿）是新的，语音（音频，从缓存拿）是旧的。

    实测（2026-10-21）：服务端音频已被重跑成 184s 的新版，客户端仍播缓存里
    223s 的旧版，进度条停在 `3:43 / 3:43`（服务端文件实际是 3:04），
    用户反馈「内容对、语音不对」。

    key 里带内容 hash 后：内容变 → key 变 → URL 变 → 缓存自然失效。

    这对 local 和 oss 都成立：
    - local：文件名变 → 静态目录里的文件变 → URL 变
    - oss：presigned URL 由 key 决定 → key 变 → URL 变
      （presigned URL **不能**额外拼 query —— 那会破坏签名）

    ## 代价

    同一篇文章的历史版本文件会变成孤儿（文件名带旧 hash）。
    清理见 content-service/article_purge.py（按 DB 里的实际 key 删）。
    """
    return f"audio/{article_id}.{content_hash(data)}.{ext}"


def versioned_variant_key(article_id: str, bitrate: int, fmt: str, data: bytes) -> str:
    """内容寻址的变体 key：`audio/{article_id}.{bitrate}k.{hash8}.{fmt}`。

    变体（多码率 warm）同样会被重新生成，理由同 versioned_audio_key。
    """
    return f"audio/{article_id}.{bitrate}k.{content_hash(data)}.{fmt}"


class Storage(ABC):
    """文件存储抽象。

    实现类：
    - LocalStorage（开发用，存本地磁盘）
    - OSSStorage（生产用，留接口）
    """

    @abstractmethod
    async def save(
        self,
        key: str,
        data: bytes,
        content_type: str = "audio/mpeg",
    ) -> str:
        """存文件 + 返回公开 URL。

        Args:
            key: 文件 key（如 "audio/{article_id}.mp3"）
            data: 文件字节
            content_type: MIME 类型

        Returns:
            公开 URL（前端可访问）
        """
        pass

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """检查文件是否存在。"""
        pass

    @abstractmethod
    async def delete(self, key: str) -> None:
        """删文件（幂等：文件不存在时静默返回，不抛错）。

        用于文章硬删除时清理蒸馏音频。key 与 save 一致 ——
        音频 key 现在带内容 hash（见 versioned_audio_key），
        所以调用方要从 DB 里的实际 audio_url / oss_key 反推，不能按模板硬拼。
        """
        pass

    async def fetch(self, key: str) -> bytes:
        """读文件内容（CP7.3.0：按需转码需拉回主音频 bytes）。

        非抽象默认方法：实现方按需覆盖（LocalStorage 已覆盖）。
        未实现 → NotImplementedError（调用方兜底 log + 返回 None，不破主流程）。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 fetch()")

    def key_from_url(self, url: str) -> str:
        """从公开 URL 反推 storage key（CP7.3.0：variants 接口按码率找文件）。

        默认启发式实现（OSS 等未覆盖时也能用）：
        - 含已知 CDN 主机标记（aliyuncs.com 等）→ 取 hostname 之后的完整 path
        - 否则（dev gateway 代理 URL http://gw:8100/audio/audio/xx.m4a）
          → 取最后两段（audio/<file>），覆盖 LocalStorage 单层 key 的常见形态

        LocalStorage 精确覆盖（直接剥 public_url_base 前缀）。
        """
        from urllib.parse import unquote, urlparse

        parsed = urlparse(url)
        path = unquote(parsed.path).lstrip("/")
        host = parsed.hostname or ""
        if any(mark in host for mark in ("aliyuncs.com", "myqcloud.com", "oss-", "cos.")):
            return path
        # 通用兜底：最后两段足够覆盖 audio/{article_id}.m4a 形态
        parts = path.split("/")
        return "/".join(parts[-2:]) if len(parts) >= 2 else path
