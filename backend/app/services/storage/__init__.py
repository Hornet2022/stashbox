"""Storage 工厂。"""

import os
from .base import Storage
from .local import LocalStorage
from .oss import OSSStorage


def get_storage() -> Storage:
    """根据 STORAGE_PROVIDER 返回对应 storage。

    CP9.x：本机 dev 模式 storage URL 必须从 PUBLIC_GATEWAY_URL 拼出（不能
    硬编码 localhost，真机 localhost = 设备自己）。LOCAL_STORAGE_PUBLIC_URL
    没设时显式传 None，让 LocalStorage 自己用 PUBLIC_GATEWAY_URL 兜底。

    ⚠️ 缺省值从 "local" 改成 "oss"（2026-10-02）：同一个环境变量原本三处默认值
    不一致 —— 这里 local，而 `common/config.py:122`（Settings.storage_provider）
    和 `api-gateway/main.py:140` 都是 oss。生产 `.env` 恰好**没设**这个变量，
    于是 ai-service 拿到 LocalStorage 去 /tmp/audio 找主音频，而音频在
    SeaweedFS 里 → 多码率转码 100% 静默失败（`article_audio_variants` 恒 0 条）。
    少数派是这里，跟另外两处对齐。

    读 os.getenv 而不是 settings.storage_provider：common/config.py 在 import
    时已 `load_dotenv(_ENV_FILE)` 把 .env 灌进 os.environ，两种读法等价；
    保持 os.getenv 是为了这个模块不依赖 pydantic Settings 的构造顺序。
    """
    provider = os.getenv("STORAGE_PROVIDER", "oss").lower()

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
