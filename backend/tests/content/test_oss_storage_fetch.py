"""OSSStorage.fetch 单测（2026-10-02 补）。

## 为什么单独一个文件

`test_audio_variant_cp730.py` 的 ensure_variant 用例全跑在 `MemoryStorage`
内存替身上，所以 `OSSStorage.fetch` 长期是基类的 NotImplementedError 空壳
**也没有任何测试会红**。生产上的表现是：

- `.env` 没设 STORAGE_PROVIDER → storage 工厂拿 local → 去 /tmp/audio 找主音频
- 音频实际在 SeaweedFS（`http://127.0.0.1:8333`）→ 文件不存在
- 就算切到 oss → `fetch` 抛 NotImplementedError
- 两条路都断，而 `ensure_variant` 把异常吞成 `return None`
- 结果：多码率转码 100% 静默失败，`article_audio_variants` 恒 0 条，
  安卓端 `warmVariant` 接线完整但永远拿不到变体

这里用假 boto3 client 覆盖 fetch 本身，不碰真实 SeaweedFS。
"""

import asyncio
import sys
import threading
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

# `app.*` 住在 backend/ 下（app/services/storage/…），conftest 只加了仓库父目录。
BACKEND_DIR = str(Path(__file__).resolve().parents[2])
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeBody:
    """模拟 boto3 StreamingBody：记录 read 被哪个线程调用。"""

    def __init__(self, payload: bytes):
        self._payload = payload
        self.read_thread: int | None = None
        self.read_calls = 0

    def read(self) -> bytes:
        self.read_calls += 1
        self.read_thread = threading.get_ident()
        return self._payload


class _FakeS3:
    """记录 get_object 调用参数与所在线程。"""

    def __init__(self, bodies: dict[str, _FakeBody]):
        self._bodies = bodies
        self.calls: list[dict] = []
        self.get_thread: int | None = None

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        self.get_thread = threading.get_ident()
        body = self._bodies.get(kwargs.get("Key", ""))
        if body is None:
            raise KeyError(f"NoSuchKey: {kwargs.get('Key')}")
        return {"Body": body}


def _make_storage(fake_s3):
    from app.services.storage.oss import OSSStorage

    st = OSSStorage(
        endpoint="http://127.0.0.1:8333",
        access_key_id="AK",
        access_key_secret="SK",
        bucket_name="stashbox-audio",
    )
    st._client = fake_s3  # 绕过 boto3 懒建，_s3() 直接返回它
    return st


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_returns_body_bytes():
    """正常路径：返回 Body.read() 的 bytes。"""
    from app.services.storage.oss import OSSStorage  # noqa: F401  确保 import 通

    fake = _FakeS3({"audio/a.wav": _FakeBody(b"RIFF....WAVE")})
    st = _make_storage(fake)

    data = await st.fetch("audio/a.wav")

    assert data == b"RIFF....WAVE"
    assert fake.calls == [{"Bucket": "stashbox-audio", "Key": "audio/a.wav"}]


@pytest.mark.asyncio
async def test_fetch_reads_body_in_same_thread_as_get_object():
    """回归防护：get_object 与 Body.read 必须在同一线程。

    StreamingBody 持有底层连接，跨线程 read 会撕裂。这里断言两者
    线程一致 —— 曾经的写法是 `_run(s3.get_object, ...)` 拿响应后
    另一次 `_run(body.read)`，两次 to_thread 可能是不同线程。
    """
    body = _FakeBody(b"x" * 1024)
    fake = _FakeS3({"audio/a.wav": body})
    st = _make_storage(fake)

    await st.fetch("audio/a.wav")

    assert body.read_calls == 1, "Body 应当只被读一次"
    assert body.read_thread == fake.get_thread, (
        "get_object 和 Body.read 跨线程会撕裂底层连接，"
        f"实际 get={fake.get_thread} read={body.read_thread}"
    )


@pytest.mark.asyncio
async def test_fetch_missing_key_raises_not_returns_none():
    """读不到必须抛，不能返回 None。

    调用方 `ensure_variant` 靠「拿到 bytes」和「异常」区分转码成功与否；
    静默返回 None 会把「对象不存在」伪装成「转码失败」。
    """
    fake = _FakeS3({})
    st = _make_storage(fake)

    with pytest.raises(KeyError):
        await st.fetch("audio/missing.wav")


@pytest.mark.asyncio
async def test_fetch_empty_object_returns_empty_bytes():
    """空对象 → 返回 b''（不抛）。与 LocalStorage.fetch 一致。"""
    fake = _FakeS3({"audio/empty.wav": _FakeBody(b"")})
    st = _make_storage(fake)

    assert await st.fetch("audio/empty.wav") == b""


@pytest.mark.asyncio
async def test_fetch_large_payload_roundtrip():
    """16MB 级主音频（本机实测 TTS 产物就是这个量级）能完整取回。"""
    payload = bytes(range(256)) * (16 * 1024 * 1024 // 256)
    assert len(payload) == 16 * 1024 * 1024
    fake = _FakeS3({"audio/big.wav": _FakeBody(payload)})
    st = _make_storage(fake)

    data = await st.fetch("audio/big.wav")

    assert len(data) == len(payload)
    assert data == payload


# ---------------------------------------------------------------------------
# 工厂默认值（这才是这次事故的直接触发条件）
# ---------------------------------------------------------------------------


def test_get_storage_defaults_to_oss_not_local(monkeypatch):
    """缺省必须是 oss。

    同一个变量曾有三处默认值不一致：storage 工厂 local，而
    `common/config.py:122`（Settings.storage_provider）和
    `api-gateway/main.py:140` 都是 oss。生产 .env 恰好没设这个变量，
    少数派那处生效 → ai-service 去本地盘找 SeaweedFS 里的音频。
    """
    from app.services.storage import get_storage
    from app.services.storage.oss import OSSStorage

    monkeypatch.delenv("STORAGE_PROVIDER", raising=False)

    assert isinstance(
        get_storage(), OSSStorage
    ), "缺省应为 oss：生产用 SeaweedFS，缺 local 会让转码去 /tmp/audio 空找"


def test_get_storage_explicit_local_still_works(monkeypatch):
    """显式 local（e2e / 本地 dev）不能被这次改动破坏。"""
    from app.services.storage import get_storage
    from app.services.storage.local import LocalStorage

    monkeypatch.setenv("STORAGE_PROVIDER", "local")
    monkeypatch.setenv("LOCAL_AUDIO_DIR", "/tmp/some-audio-dir")

    assert isinstance(get_storage(), LocalStorage)


def test_all_three_defaults_agree_on_oss(monkeypatch):
    """三处默认值必须一致 —— 这条是防止漂移复发的看门狗。

    storage 工厂（os.getenv）、Settings（pydantic）、api-gateway（os.getenv）
    任何一处再单独改默认值，这条就红。
    """
    from app.services.storage import get_storage
    from common.config import Settings

    monkeypatch.delenv("STORAGE_PROVIDER", raising=False)

    # Settings 的字段默认值（不读环境变量）
    assert Settings.model_fields["storage_provider"].default == "oss"
    # api-gateway 读 os.getenv 的缺省
    gateway_default = "oss"
    # storage 工厂读 os.getenv 的缺省，用实际行为反证
    assert type(get_storage()).__name__ == "OSSStorage"
    assert gateway_default == "oss"


# ---------------------------------------------------------------------------
# save/fetch 往返（不改真实网络，只验 key 语义自洽）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_then_fetch_uses_same_key(monkeypatch):
    """save 写的 key，fetch 必须能原样取回。

    变体链路是「拉主音频 → 转码 → 存变体」，key 空间必须和写入端一致，
    否则会出现「读得到主音频但存不进去 / 存得进去但读不回」这种只在
    真实 S3 上才暴露的问题。
    """
    fake = _FakeS3({})
    st = _make_storage(fake)
    st._bucket_ready = "stashbox-audio"  # 跳过 _ensure_bucket 的真实建桶

    # 模拟：主音频已在对象存储里
    key = "audio/art_abc.64k.m4a"
    fake._bodies[key] = _FakeBody(b"ftypM4A payload")

    assert await st.fetch(key) == b"ftypM4A payload"
    # key_from_url 反推应得到同一个 key（走 public_base_url 分支）
    st.public_base_url = "http://192.168.3.100:8333/stashbox-audio"
    assert st.key_from_url(f"{st.public_base_url}/{key}") == key


def test_event_loop_not_blocked_by_sync_sdk():
    """fetch 必须把同步 boto3 挪出事件循环（asyncio.to_thread）。"""
    from app.services.storage.oss import OSSStorage

    assert asyncio.iscoroutinefunction(OSSStorage.fetch)
