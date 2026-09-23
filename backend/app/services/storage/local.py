"""本地存储（开发用）。存到 /tmp/audio/。"""

import os

import aiofiles
from pathlib import Path
from .base import Storage


class LocalStorage(Storage):
    """本地存储。

    存到 /tmp/audio/{key}（或 LOCAL_AUDIO_DIR 覆盖），
    返回 {public_url_base}/{key} URL。
    配合 backend/api-gateway 加静态文件服务路由。

    CP9.x：public URL 必须是客户端可达的地址，不能是 localhost
    （真机的 localhost 指向设备自己，会 404）。
    优先级：
      1. 显式传入的 public_url_base
      2. LOCAL_STORAGE_PUBLIC_URL env
      3. PUBLIC_GATEWAY_URL env + '/audio'（与 content-service 端点重写共用一份 env）
      4. 默认 http://localhost:8100/audio（兜底，仅模拟器内可用）
    """

    def __init__(
        self,
        base_dir: str | None = None,
        public_url_base: str | None = None,
    ):
        self.base_dir = Path(base_dir or os.getenv("LOCAL_AUDIO_DIR", "/tmp/audio"))
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Android 真机/模拟器地址不同：emulator→10.0.2.2，真机→局域网 IP（如 172.16.x.x）
        # CP9.x：必须从 env 读，避免硬编码导致 Android 拿不到音频
        if public_url_base is not None:
            self.public_url_base = public_url_base.rstrip("/")
        else:
            explicit = os.getenv("LOCAL_STORAGE_PUBLIC_URL")
            if explicit:
                self.public_url_base = explicit.rstrip("/")
            else:
                # 兜底：用 PUBLIC_GATEWAY_URL（与 content-service 共用同个 env）
                # + '/audio' 拼出 storage URL。
                gateway = os.getenv("PUBLIC_GATEWAY_URL", "http://localhost:8100")
                self.public_url_base = f"{gateway.rstrip('/')}/audio"

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

    async def delete(self, key: str) -> None:
        """删本地文件。missing_ok=True → 幂等，不存在不抛错。"""
        (self.base_dir / key).unlink(missing_ok=True)
