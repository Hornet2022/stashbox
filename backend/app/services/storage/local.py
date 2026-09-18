"""本地存储（开发用）。存到 /tmp/audio/。"""

import aiofiles
from pathlib import Path
from .base import Storage


class LocalStorage(Storage):
    """本地存储。

    存到 /tmp/audio/{key}，返回 http://localhost:8100/audio/{key} URL。
    配合 backend/api-gateway 加静态文件服务路由。
    """

    def __init__(
        self, base_dir: str = "/tmp/audio", public_url_base: str = "http://localhost:8100/audio"
    ):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.public_url_base = public_url_base.rstrip("/")

    async def save(
        self,
        key: str,
        data: bytes,
        content_type: str = "audio/mpeg",
    ) -> str:
        file_path = self.base_dir / key
        file_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiofiles.open(file_path, "wb") as f:
            await f.write(data)

        return f"{self.public_url_base}/{key}"

    async def exists(self, key: str) -> bool:
        return (self.base_dir / key).exists()
