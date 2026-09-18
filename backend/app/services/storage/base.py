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
