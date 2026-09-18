"""Storage 工厂。"""

import os
from .base import Storage
from .local import LocalStorage
from .oss import OSSStorage


def get_storage() -> Storage:
    """根据 settings.storage_provider 返回对应 storage。"""
    provider = os.getenv("STORAGE_PROVIDER", "local").lower()

    if provider == "local":
        return LocalStorage()
    elif provider == "oss":
        return OSSStorage(
            access_key_id=os.getenv("OSS_ACCESS_KEY_ID", ""),
            access_key_secret=os.getenv("OSS_ACCESS_KEY_SECRET", ""),
            endpoint=os.getenv("OSS_ENDPOINT", ""),
            bucket_name=os.getenv("OSS_BUCKET_NAME", ""),
        )
    else:
        return LocalStorage()
