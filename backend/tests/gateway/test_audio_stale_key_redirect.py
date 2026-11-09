"""过期音频 key 重定向的护栏（2026-11-09）。

## 这条护栏防的是哪一类事故

音频 key 是**内容寻址**的（`{article_id}.{hash8}.{ext}`）——
重新蒸馏 ⇒ key 变 ⇒ 旧文件被删 ⇒ **客户端缓存里的旧 URL 变成 404**。

实测（2026-11-09 内测）：用户在听一篇被重新蒸馏过的文章 ⇒ 服务端 404
⇒ 客户端把它归类成「瞬时网络类」⇒ 界面显示「**网络不稳，正在重连…**」，
并在同一个永远不存在的文件上反复自动重连。
用户的原话「手机网络正常」是**对的**：网络没问题，是服务端说"这个文件没了"。

⇒ 治本 = 服务端在文件缺失时**重定向到当前版本**（302），
旧 URL 永远拿得到**当前**音频，已装客户端**不用发版**就能恢复。

## 怎么测：断言「发生了什么」，不是断言「不报错」

护栏最常见的失败方式是"写对了但没牙齿"：抽掉一条判据、让后面那条拦住，
用例照样绿。所以这里每个用例都断言具体的**响应**（302 / 200 / 404）与
**Location 头**，而不是"有没有抛异常"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.exceptions import HTTPException as StarletteHTTPException

from stashbox.backend.app.services.audio.resolve import current_audio_key
from stashbox.backend.app.services.audio.stale_key import (
    MOUNT_PREFIX,
    is_stale_request,
    key_from_audio_url,
    parse_audio_key,
)
from stashbox.backend.app.services.audio.static_files import (
    AudioFilesWithStaleRedirect,
    build_stale_resolver,
)

STALE = "audio/art_3dd88aca36f645c6aee0a5be.4c67fadb.mp3"
CURRENT = "audio/art_3dd88aca36f645c6aee0a5be.50967cd2.mp3"


# ── 纯函数：key 解析 ────────────────────────────────────────────────
def test_parse_audio_key_识别带内容hash的音频key():
    parsed = parse_audio_key(STALE)
    assert parsed is not None
    assert parsed.article_id == "art_3dd88aca36f645c6aee0a5be"
    assert parsed.content_hash == "4c67fadb"
    assert parsed.ext == "mp3"
    assert parsed.key == STALE
    assert parsed.public_path == f"{MOUNT_PREFIX}/{STALE}"


def test_parse_audio_key_不是音频key就返回None():
    # 静态目录里还有别的东西 —— 不能一律当成音频去查库
    assert parse_audio_key("blobs/whatever.txt") is None
    assert parse_audio_key("audio/notahash.mp3") is None
    assert parse_audio_key("audio/art_x.4c67fadb.mp3.bak") is None
    # 内容 hash 是**十六进制**（sha256 前若干位）⇒ 非十六进制的一律不认
    assert parse_audio_key("audio/art_x.ZZZ.mp3") is None
    assert parse_audio_key("") is None


def test_key_from_audio_url_取最后一段并去掉query():
    assert (
        key_from_audio_url("https://api.stashbox.cn/audio/audio/art_x.4c67fadb.mp3")
        == "audio/art_x.4c67fadb.mp3"
    )
    assert (
        key_from_audio_url("https://api.stashbox.cn/audio/audio/art_x.4c67fadb.mp3?v=2")
        == "audio/art_x.4c67fadb.mp3"
    )
    assert key_from_audio_url(None) is None
    assert key_from_audio_url("") is None
    assert key_from_audio_url("https://api.stashbox.cn/not-an-audio") is None


def test_is_stale_request_只有当前key不同才算过期():
    assert is_stale_request(STALE, CURRENT) is True
    assert is_stale_request(STALE, STALE) is False
    # 没有当前音频（文章被删 / 从未生成）⇒ 不是"过期"，该 404 就 404
    assert is_stale_request(STALE, None) is False


# ── 解析器（注入式，可测）───────────────────────────────────────────
def _resolver(lookup_result, *, lookup_error: Exception | None = None):
    async def _lookup(article_id: str) -> str | None:
        if lookup_error is not None:
            raise lookup_error
        return lookup_result

    return build_stale_resolver(
        _lookup,
        parse=parse_audio_key,
        is_stale=is_stale_request,
        mount_prefix=MOUNT_PREFIX,
    )


async def test_解析器_过期key解析到当前音频的公开路径():
    resolver = _resolver(CURRENT)
    assert await resolver(STALE) == f"{MOUNT_PREFIX}/{CURRENT}"


async def test_解析器_当前key相同就不重定向():
    resolver = _resolver(STALE)
    assert await resolver(STALE) is None


async def test_解析器_没有当前音频就不重定向():
    resolver = _resolver(None)
    assert await resolver(STALE) is None


async def test_解析器_非音频路径不重定向():
    resolver = _resolver(CURRENT)
    assert await resolver("blobs/x.txt") is None


async def test_解析器_查库失败按没有处理而不是抛异常():
    """静态文件服务不该因为查库失败就 500。"""
    resolver = _resolver(None, lookup_error=RuntimeError("db down"))
    assert await resolver(STALE) is None


# ── 行为：StaticFiles 子类 ───────────────────────────────────────────
async def _get_response(directory: Path, path: str, resolver):
    app = AudioFilesWithStaleRedirect(
        directory=str(directory),
        check_dir=False,
        stale_resolver=resolver,
    )
    scope = {"type": "http", "method": "GET", "path": f"/audio/{path}", "headers": []}
    return await app.get_response(path, scope)


async def test_文件存在时照常返回不重定向(tmp_path):
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "art_x.4c67fadb.mp3").write_bytes(b"audio-bytes")
    resolver = _resolver(CURRENT)
    response = await _get_response(tmp_path, "audio/art_x.4c67fadb.mp3", resolver)
    assert response.status_code == 200
    # 命中的必须是磁盘上那个文件（不是重定向）
    assert str(response.path).endswith("art_x.4c67fadb.mp3")


async def test_文件缺失且key过期时返回302并指向当前版本(tmp_path):
    (tmp_path / "audio").mkdir()
    resolver = _resolver(CURRENT)
    response = await _get_response(tmp_path, STALE, resolver)
    assert response.status_code == 302
    assert response.headers["location"] == f"{MOUNT_PREFIX}/{CURRENT}"


async def test_文件缺失且无法判定过期时仍是404(tmp_path):
    (tmp_path / "audio").mkdir()
    resolver = _resolver(None)
    with pytest.raises(StarletteHTTPException) as exc:
        await _get_response(tmp_path, STALE, resolver)
    assert exc.value.status_code == 404


async def test_没给解析器时行为与普通静态目录一致(tmp_path):
    (tmp_path / "audio").mkdir()
    app = AudioFilesWithStaleRedirect(directory=str(tmp_path), check_dir=False)
    scope = {"type": "http", "method": "GET", "path": f"/audio/{STALE}", "headers": []}
    with pytest.raises(StarletteHTTPException) as exc:
        await app.get_response(STALE, scope)
    assert exc.value.status_code == 404


# ── 接线护栏：网关必须真的用上这个类 ────────────────────────────────
def test_接线护栏_网关挂载用的是带重定向的静态目录类():
    """只看代码没用 —— 必须确认**网关**真的挂的是这个类。"""
    main_py = Path(__file__).resolve().parents[2] / "api-gateway" / "main.py"
    text = main_py.read_text(encoding="utf-8")
    assert "AudioFilesWithStaleRedirect" in text
    assert "stale_resolver=" in text
    # 挂载必须带上解析器（否则类退化成普通静态目录，护栏形同虚设）
    assert "build_stale_resolver(" in text


# ── DB 查询入口（用假 session 测 SQL 口径）──────────────────────────
class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """记录被执行的语句，按顺序返回预设结果。"""

    def __init__(self, *results):
        self._results = list(results)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        value = self._results.pop(0) if self._results else None
        return _FakeResult(value)


async def test_当前音频key_优先取蒸馏行():
    session = _FakeSession(
        "https://api.stashbox.cn/audio/audio/art_x.50967cd2.mp3",
    )
    key = await current_audio_key(session, "art_x")
    assert key == "audio/art_x.50967cd2.mp3"
    assert len(session.statements) == 1  # 蒸馏行命中 ⇒ 不再查主档


async def test_当前音频key_蒸馏行没有时兜底主档():
    session = _FakeSession(None, "https://api.stashbox.cn/audio/audio/art_x.50967cd2.mp3")
    key = await current_audio_key(session, "art_x")
    assert key == "audio/art_x.50967cd2.mp3"
    assert len(session.statements) == 2


async def test_当前音频key_两处都没有时返回None():
    session = _FakeSession(None, None)
    assert await current_audio_key(session, "art_x") is None
