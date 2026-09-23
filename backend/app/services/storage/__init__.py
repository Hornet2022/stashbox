"""Storage 工厂。"""

import os
from .base import Storage
from .local import LocalStorage
from .oss import OSSStorage


def get_storage() -> Storage:
    """根据 settings.storage_provider 返回对应 storage。

    CP9.x：本机 dev 模式 storage URL 必须从 PUBLIC_GATEWAY_URL 拼出（不能
    硬编码 localhost，真机 localhost = 设备自己）。LOCAL_STORAGE_PUBLIC_URL
    没设时显式传 None，让 LocalStorage 自己用 PUBLIC_GATEWAY_URL 兜底。
    """
    provider = os.getenv("STORAGE_PROVIDER", "local").lower()

    if provider == "local":
        return LocalStorage(
            base_dir=os.getenv("LOCAL_AUDIO_DIR", "/tmp/audio"),
            public_url_base=os.getenv(
                "LOCAL_STORAGE_PUBLIC_URL"
            ),  # 没设 → None → 走 PUBLIC_GATEWAY_URL
        )
    elif provider == "oss":
        return OSSStorage(
            access_key_id=os.getenv("OSS_ACCESS_KEY_ID", ""),
            access_key_secret=os.getenv("OSS_ACCESS_KEY_SECRET", ""),
            endpoint=os.getenv("OSS_ENDPOINT", ""),
            bucket_name=os.getenv("OSS_BUCKET", ""),
        )
    else:
        return LocalStorage()
