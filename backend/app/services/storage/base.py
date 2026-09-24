"""Storage 抽象接口。"""

from abc import ABC, abstractmethod


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

        用于文章硬删除时清理蒸馏音频（key 同 save，如 "audio/{article_id}.wav"）。
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
