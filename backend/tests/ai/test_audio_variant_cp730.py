"""CP7.3.0：多码率音频变体（按需转码路线 B）单测。

覆盖：
- _variant_key / 常量: 2 cases
- key_from_url（base 启发式 + LocalStorage 精确）: 4 cases
- transcode_bytes + probe_duration_sec（真 ffmpeg，1s 正弦源）: 3 cases
- ensure_variant（内存 storage + SQLite 全链路 / 幂等 / 各失败兜底）: 6 cases
- list_variants（主档 + 缺档补齐 + 失败降级）: 3 cases
"""

import asyncio
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-service"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class MemoryStorage:
    """dict 内存 storage（实现抽象三件套 + fetch；key_from_url 继承基类）。"""

    def __init__(self, support_fetch=True):
        self.files: dict[str, bytes] = {}
        self._support_fetch = support_fetch

    async def save(self, key, data, content_type="audio/mpeg"):
        self.files[key] = data
        return f"https://mem.example/{key}"

    async def exists(self, key):
        return key in self.files

    async def delete(self, key):
        self.files.pop(key, None)

    async def fetch(self, key):
        if not self._support_fetch:
            raise NotImplementedError("OSSStorage 未实现 fetch()")
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]


# 兼容基类（Storage.key_from_url 默认实现被复用）
@pytest.fixture(autouse=True)
def _no_real_transcode_enqueue(monkeypatch):
    """把转码入队 stub 掉。

    2026-10-02 起 list_variants 会调 ``_enqueue_transcode`` → ``get_dispatcher()``
    建**全局** Redis 连接池。如果让单测真建，那个池会绑在本测试的 event loop 上；
    循环结束后全局单例仍持有已关闭的连接，后续用例会报
    ``RuntimeError: Event loop is closed``（实测污染到 test_distill_dispatcher）。

    单测本来就不该连 Redis，这里 stub 掉顺带让本文件变成纯离线单测。
    """

    async def _fake(article_id, bitrate):
        return None

    monkeypatch.setattr("distill.audio_variant_service._enqueue_transcode", _fake, raising=True)


from stashbox.backend.app.services.storage.base import Storage  # noqa: E402

MemoryStorage.key_from_url = Storage.key_from_url


def _sine_m4a(tmp_dir: Path, seconds: int = 1) -> bytes:
    """真 ffmpeg 生成测试源音频（128k mono AAC）。"""
    out = tmp_dir / f"src_{uuid.uuid4().hex[:6]}.m4a"
    proc = subprocess.run(
        [
            "/opt/homebrew/bin/ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "1",
            str(out),
        ],
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr[-300:]
    data = out.read_bytes()
    out.unlink()
    return data


async def _make_db():
    """SQLite 内存库：只建 article_audio_variants 表（CP3.7.3 同款套路）。

    DistilledArticle 用纯内存对象传入（不落库）——articles 表含 JSONB
    （raw_content）SQLite 编不过，而 variants 表 FK 在 SQLite 默认不强制，
    不需要真父表。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from stashbox.backend.common.models.article_audio_variant import (
        ArticleAudioVariant,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: ArticleAudioVariant.__table__.create(c, checkfirst=True))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _da(audio_url="https://gw.example/audio/audio/art1.m4a", **kw):
    from stashbox.backend.common.models import DistilledArticle

    defaults = dict(
        id=f"dst_{uuid.uuid4().hex[:24]}",
        article_id=kw.pop("article_id", f"art_{uuid.uuid4().hex[:20]}"),
        status="done",
        audio_url=audio_url,
        duration_sec=300,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    defaults.update(kw)
    return DistilledArticle(**defaults)


# ---------------------------------------------------------------------------
# 1. 纯函数
# ---------------------------------------------------------------------------


def test_variant_key_format():
    from distill.audio_variant_service import _variant_key

    assert _variant_key("art1", 64) == "audio/art1.64k.m4a"
    assert _variant_key("art1", 96, "mp3") == "audio/art1.96k.mp3"


def test_bitrate_constants():
    from distill.audio_variant_service import (
        ALL_BITRATES,
        MAIN_BITRATE,
        TRANSCODE_BITRATES,
    )

    assert MAIN_BITRATE == 128
    assert TRANSCODE_BITRATES == (96, 64)
    assert ALL_BITRATES == (128, 96, 64)


# ---------------------------------------------------------------------------
# 2. key_from_url
# ---------------------------------------------------------------------------


def test_key_from_url_generic_oss():
    """OSS CDN 主机 → hostname 后完整 path。"""
    from stashbox.backend.app.services.storage import get_storage  # noqa: F401

    s = MemoryStorage()
    assert (
        s.key_from_url("https://b.oss-cn-hangzhou.aliyuncs.com/audio/art1.m4a") == "audio/art1.m4a"
    )


def test_key_from_url_dev_proxy_last_two():
    """dev gateway 代理 URL → 取最后两段。"""
    s = MemoryStorage()
    assert s.key_from_url("http://localhost:8100/audio/audio/art1.m4a") == "audio/art1.m4a"


def test_key_from_url_local_exact_prefix():
    """LocalStorage：剥 public_url_base 前缀。"""
    from stashbox.backend.app.services.storage.local import LocalStorage

    ls = LocalStorage(base_dir="/tmp/audio_test_cp730", public_url_base="http://gw:8100/audio")
    assert ls.key_from_url("http://gw:8100/audio/audio/x.m4a") == "audio/x.m4a"
    # 前缀不匹配 → 基类兜底
    assert ls.key_from_url("https://cdn.other/a/b/c.m4a") == "b/c.m4a"


def test_base_fetch_not_implemented():
    """基类 fetch 默认抛 NotImplementedError（OSSStorage 未覆盖即如此）。"""

    class _NoFetch(Storage):
        async def save(self, key, data, content_type="audio/mpeg"):
            return ""

        async def exists(self, key):
            return False

        async def delete(self, key):
            pass

    with pytest.raises(NotImplementedError):
        asyncio.run(_NoFetch().fetch("k"))


# ---------------------------------------------------------------------------
# 3. transcode + probe（真 ffmpeg）
# ---------------------------------------------------------------------------


def test_transcode_and_probe(tmp_path):
    from distill.audio_variant_service import probe_duration_sec, transcode_bytes

    src = _sine_m4a(tmp_path, seconds=2)
    out = asyncio.run(transcode_bytes(src, 64))
    assert len(out) > 0
    assert len(out) < len(src) * 2  # 粗校验（64k 应比 128k 小，留编码余量）
    dur = asyncio.run(probe_duration_sec(out))
    assert dur == 2


def test_transcode_bitrate_ordering(tmp_path):
    """96k 产物应大于 64k 产物（码率生效）。"""
    from distill.audio_variant_service import transcode_bytes

    src = _sine_m4a(tmp_path, seconds=3)
    o96 = asyncio.run(transcode_bytes(src, 96))
    o64 = asyncio.run(transcode_bytes(src, 64))
    assert len(o96) > len(o64)


def test_transcode_bad_bytes_raises(tmp_path):
    """垃圾输入 → RuntimeError（调用方兜底不抛穿）。"""
    from distill.audio_variant_service import transcode_bytes

    with pytest.raises(Exception):
        asyncio.run(transcode_bytes(b"not-audio" * 10, 64))


# ---------------------------------------------------------------------------
# 4. ensure_variant（全链路 + 兜底）
# ---------------------------------------------------------------------------


async def test_ensure_variant_full_chain(tmp_path):
    """fetch 主音频 → 转码 → save → 写 DB 行；幂等二次命中。"""
    from distill.audio_variant_service import AudioVariantService, _variant_key

    src = _sine_m4a(tmp_path, seconds=1)
    st = MemoryStorage()
    da = _da(audio_url="https://gw.example/audio/audio/main1.m4a")
    st.files["audio/main1.m4a"] = src

    engine, sf = await _make_db()
    async with sf() as db:
        svc = AudioVariantService(storage=st, session_factory=sf)
        row = await svc.ensure_variant(db, da, 64)
        assert row is not None
        assert row.bitrate == 64
        assert row.oss_key == _variant_key(da.article_id, 64)
        assert row.file_size_bytes > 0
        assert row.duration_sec == 1  # probe 优先于 da.duration_sec=300
        assert row.mono is True
        assert _variant_key(da.article_id, 64) in st.files

        # 幂等：第二次不再转码（若重转，save 会被调用但结果一致；关键是不产生新行）
        row2 = await svc.ensure_variant(db, da, 64)
        assert row2.id == row.id
    await engine.dispose()


async def test_ensure_variant_invalid_bitrate():
    from distill.audio_variant_service import AudioVariantService

    svc = AudioVariantService(storage=MemoryStorage())
    assert await svc.ensure_variant(None, _da(), 128) is None  # 主档不转码


async def test_ensure_variant_no_audio_url():
    """mock 路径 audio_url=None → 无源可转，None。"""
    from distill.audio_variant_service import AudioVariantService

    svc = AudioVariantService(storage=MemoryStorage())
    engine, sf = await _make_db()
    async with sf() as db:
        assert await svc.ensure_variant(db, _da(audio_url=None), 64) is None
    await engine.dispose()


async def test_ensure_variant_fetch_not_supported():
    """OSS 未实现 fetch → 兜底 None（log warning，不抛）。"""
    from distill.audio_variant_service import AudioVariantService

    svc = AudioVariantService(storage=MemoryStorage(support_fetch=False))
    engine, sf = await _make_db()
    async with sf() as db:
        assert await svc.ensure_variant(db, _da(), 64) is None
    await engine.dispose()


async def test_ensure_variant_missing_main_file():
    """storage 里没有主音频文件 → fetch FileNotFoundError → None。"""
    from distill.audio_variant_service import AudioVariantService

    svc = AudioVariantService(storage=MemoryStorage())
    engine, sf = await _make_db()
    async with sf() as db:
        assert await svc.ensure_variant(db, _da(), 64) is None
    await engine.dispose()


# ---------------------------------------------------------------------------
# 5. list_variants
# ---------------------------------------------------------------------------


async def test_list_variants_does_not_transcode_synchronously(tmp_path):
    """2026-10-02 起：读接口**不再**同步转码。

    原来这里是 `assert all(v["available"])` —— 即「调一次列表就把缺的档转出来」。
    问题是这个读接口是用户点开文章详情页时调的，而转 30 分钟音频要几十秒到
    几分钟，安卓端超时 65s：地铁隧道里打开一篇只有主档的文章必然超时失败，
    弱网反而比强网更糟。

    新契约：缺档时 available=false（主档立刻可用），转码交给 arq 后台任务。
    这正是产品化方案 §5 决策 2 的选项 B「按需转码」该有的样子。
    """
    from distill.audio_variant_service import AudioVariantService

    src = _sine_m4a(tmp_path, seconds=1)
    st = MemoryStorage()
    da = _da(audio_url="https://mem.example/audio/main2.m4a")
    st.files["audio/main2.m4a"] = src

    engine, sf = await _make_db()
    async with sf() as db:
        svc = AudioVariantService(storage=st, session_factory=sf)
        vs = await svc.list_variants(db, da)
        assert [v["bitrate"] for v in vs] == [128, 96, 64]
        # 主档照常可用 —— 用户当下能播
        assert vs[0]["is_main"] and vs[0]["available"]
        assert vs[0]["url"] == da.audio_url
        # 低码率不在读请求里转，报告为未就绪
        assert [v["available"] for v in vs] == [True, False, False]
    await engine.dispose()


async def test_list_variants_transcode_failure_degrades(tmp_path):
    """fetch 不支持（生产 OSS 未实现）→ 96/64 available=false，主档不受影响。"""
    from distill.audio_variant_service import AudioVariantService

    st = MemoryStorage(support_fetch=False)
    da = _da(audio_url="https://gw.example/audio/audio/main3.m4a")
    engine, sf = await _make_db()
    async with sf() as db:
        svc = AudioVariantService(storage=st, session_factory=sf)
        vs = await svc.list_variants(db, da)
        assert vs[0]["available"] is True
        assert vs[1]["available"] is False and vs[1]["url"] is None
        assert vs[2]["available"] is False
    await engine.dispose()


async def test_list_variants_no_generate(tmp_path):
    """generate_missing=False（预热关闭）→ 缺档直接 false，不触发转码。"""
    from distill.audio_variant_service import AudioVariantService

    src = _sine_m4a(tmp_path, seconds=1)
    st = MemoryStorage()
    da = _da(audio_url="https://mem.example/audio/main4.m4a")
    st.files["audio/main4.m4a"] = src
    engine, sf = await _make_db()
    async with sf() as db:
        svc = AudioVariantService(storage=st, session_factory=sf)
        vs = await svc.list_variants(db, da, generate_missing=False)
        assert vs[1]["available"] is False
        assert not st.files.get("audio/unused")  # 未产生新文件
        assert all(not k.endswith((".96k.m4a", ".64k.m4a")) for k in st.files)
    await engine.dispose()
