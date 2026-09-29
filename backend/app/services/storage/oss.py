"""S3 兼容对象存储（生产用）。

CP-OSS-S3：原实现是 `raise NotImplementedError` 的空壳，导致整条蒸馏链路
永远产不出 `audio_url`：
  - `distill_task` 记 `articles.audio_url_not_http`，文章永远停在 pending；
  - agent 的 `tts_synthesize` 返回 `audio_url=None` → router 规则「有稿无音频」
    反复 `skip_to_tts`，一个任务把同一段稿子合成好几遍（实测 16 段 × 3 遍）。

现在用 **boto3（S3 协议）** 实现，可对接：
  - 本机 SeaweedFS（launchd `com.stashbox.seaweedfs`，数据在 /Volumes/DataSSD）
    endpoint: http://127.0.0.1:8333
  - MinIO / 任意 S3 兼容实现
  - 阿里云 OSS（把 endpoint 换成 OSS 域名即可，boto3 兼容）

⚠️ **不要用 `oss2`**：那是阿里云专有 SDK，与 S3 协议不兼容；SeaweedFS/MinIO
都用不了。boto3 一套代码通吃。

URL 策略：默认返回 **presigned URL**（带有效期）。
  - 公开读会让任何拿到 URL 的人长期下载音频，不做默认；
  - 需要长期直链时设 `OSS_PUBLIC_BASE_URL`，此时返回静态 URL（要求 bucket 已配公共读）。

配置来源优先级：显式入参 > 环境变量。
    OSS_ENDPOINT / OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_BUCKET
    OSS_PRESIGN_EXPIRES（秒，默认 86400）/ OSS_PUBLIC_BASE_URL
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .base import Storage

log = logging.getLogger(__name__)

# CP-OSS-ENV-PATH：自己按**模块位置**加载 backend/.env，不依赖 cwd 也不依赖
# 别人先跑过 pydantic Settings。
#
# 同类问题已在 LLM 侧踩过（`env_file=".env"` 相对 cwd，worker 在 ai-service/ 下
# 于是静默回退默认值）。OSS 这边如果也写成 `os.getenv` + 指望别处先加载 .env，
# 会出现"测试脚本能传参跑通、生产 worker 读不到配置"的漂移。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_ENV_FILE = _BACKEND_DIR / ".env"
_ENV_LOADED = False


def _ensure_env_loaded() -> None:
    """把 backend/.env 里的键补进 os.environ（已存在的环境变量优先，不覆盖）。"""
    global _ENV_LOADED
    if _ENV_LOADED or not _ENV_FILE.is_file():
        _ENV_LOADED = True
        return
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as exc:  # 读不到不该让 import 失败
        log.warning("OSS: 读取 %s 失败 err=%s", _ENV_FILE, exc)
    _ENV_LOADED = True


_CONTENT_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
}


def _guess_content_type(key: str, default: str) -> str:
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return _CONTENT_TYPES.get(ext, default)


class OSSStorage(Storage):
    """S3 兼容对象存储（boto3）。"""

    def __init__(
        self,
        access_key_id: str = "",
        access_key_secret: str = "",
        endpoint: str = "",
        bucket_name: str = "",
        *,
        presign_expires: int | None = None,
        public_base_url: str = "",
    ):
        _ensure_env_loaded()
        self.access_key_id = access_key_id or os.getenv("OSS_ACCESS_KEY_ID", "")
        self.access_key_secret = access_key_secret or os.getenv("OSS_ACCESS_KEY_SECRET", "")
        self.endpoint = (endpoint or os.getenv("OSS_ENDPOINT", "")).rstrip("/")
        self.bucket_name = bucket_name or os.getenv("OSS_BUCKET", "")
        self.presign_expires = (
            presign_expires
            if presign_expires is not None
            else int(os.getenv("OSS_PRESIGN_EXPIRES", "86400"))
        )
        self.public_base_url = (public_base_url or os.getenv("OSS_PUBLIC_BASE_URL", "")).rstrip("/")
        self._client = None
        self._bucket_ready: str | None = None

    # -- 内部 -------------------------------------------------------------
    def _s3(self):
        """懒建 boto3 S3 client（同步 SDK，放线程里跑）。"""
        if self._client is None:
            if not self.endpoint or not self.bucket_name:
                raise RuntimeError(
                    "OSS 配置缺失：需要 OSS_ENDPOINT 与 OSS_BUCKET"
                    "（本机 SeaweedFS 默认 http://127.0.0.1:8333）"
                )
            if not self.access_key_id or not self.access_key_secret:
                raise RuntimeError("OSS 凭证缺失：需要 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET")
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.access_key_secret,
                region_name=os.getenv("OSS_REGION", "us-east-1"),
                # 音频文件最大 ~15MB，单次 put_object 足够，不必分片
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=10,
                    read_timeout=120,
                ),
            )
        return self._client

    async def _run(self, fn, *args, **kwargs):
        """boto3 是同步的，放线程池避免阻塞事件循环。"""

        def _call():
            return fn(*args, **kwargs)

        return await asyncio.to_thread(_call)

    async def _ensure_bucket(self) -> None:
        """确保 bucket 存在（SeaweedFS 默认不允许自动建）。"""
        if self._bucket_ready == self.bucket_name:
            return
        s3 = self._s3()
        try:
            await self._run(s3.head_bucket, Bucket=self.bucket_name)
        except Exception:
            try:
                await self._run(s3.create_bucket, Bucket=self.bucket_name)
                log.info("OSS bucket 已创建 bucket=%s", self.bucket_name)
            except Exception as exc:
                raise RuntimeError(f"bucket {self.bucket_name!r} 不存在且创建失败: {exc}") from exc
        self._bucket_ready = self.bucket_name

    def _url_for(self, key: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{key.lstrip('/')}"
        s3 = self._s3()
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": key},
            ExpiresIn=self.presign_expires,
        )

    # -- Storage 接口 -----------------------------------------------------
    async def save(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        """上传并返回可访问 URL。

        Raises:
            RuntimeError: 配置缺失 / 网络失败 / 服务端拒绝。
        """
        if not data:
            raise ValueError(f"save({key!r}) 收到空数据")
        await self._ensure_bucket()
        s3 = self._s3()
        ctype = _guess_content_type(key, content_type)
        await self._run(
            s3.put_object,
            Bucket=self.bucket_name,
            Key=key,
            Body=data,
            ContentType=ctype,
        )
        url = self._url_for(key)
        log.info("OSS 上传成功 key=%s bytes=%d ct=%s", key, len(data), ctype)
        return url

    async def exists(self, key: str) -> bool:
        s3 = self._s3()
        try:
            await self._run(s3.head_object, Bucket=self.bucket_name, Key=key)
            return True
        except Exception:
            return False

    async def delete(self, key: str) -> None:
        s3 = self._s3()
        await self._run(s3.delete_object, Bucket=self.bucket_name, Key=key)
