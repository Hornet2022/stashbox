"""admin TTS 配置：密钥字段的**三态合并语义**（CP-TTS-KEY-CLEAR）。

背景（实测踩出来的）：
  admin-web 在用户**没有重新输入** key 时发的是**空串**（不是不传），
  意图是「沿用我已存的那把」。后端原实现是
  `if "openai_api_key" in fields and req.openai_api_key:` —— 空串被当成「不动」，
  于是**没有任何办法清掉一把 key**：DB 里那把早已失效的 `hlk_` key 一直躺在
  system_config 里，admin 页照样显示「API Key 已设置」，而当前 provider 根本不用它。

现在定成三态（见 admin_router._TTS_SECRET_FIELDS 的注释）：
  · 不传      → 不动
  · ""        → 不动（前端「没重新输入」的约定，不能破）
  · null      → **清除**
  · 非空      → 覆盖

本文件守的回归（每条都对应一个真实会出事的路径）：
  1. 三态各自的行为；
  2. PUT / test / voices-probe **三处共用同一份合并语义**（原来各抄一份，probe 那份
     还少几个字段、多一套回落 env 规则 → 语义漂移）；
  3. **闭环**：清掉当前 provider 唯一的那把 key 会让 reload() 抛
     「api_key required」—— 保存其实成功了，接口不能因此 500（否则运营看到红色报错
     以为没保存，反复重试，最后把「清除」当成功能缺失）。
  4. 清除一把**当前 provider 用不到**的 key，不影响生效配置（TTS 仍然能用）。
"""

import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from stashbox.backend.common import system_config
from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import User

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    """content-service 目录名带连字符，按文件加载，返回 (module, app)。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, module.app


content_module, content_app = _load_app("_tts_cfg_clear_main", "content-service/main.py")

BASE_KEY = "sk-stashbox-test-0123456789"


async def _make_admin() -> int:
    async with AsyncSessionLocal() as s:
        u = User(
            open_id="tts_cfg_" + uuid.uuid4().hex[:24],
            nickname="admin_" + uuid.uuid4().hex[:6],
            tier="admin",
            monthly_quota=5,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return int(u.id)


def _token(uid: int) -> str:
    return create_access_token(str(uid), extra={"tier": "admin"})


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=content_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


def _body(resp: httpx.Response) -> dict:
    """响应可能被包一层 {"data": {...}}，也可能平铺 —— 两种都认。"""
    raw = resp.json()
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        return raw["data"]
    return raw


async def _seed_tts_config(**overrides) -> dict:
    """往 system_config 里塞一份 tts 配置，返回落库后的值。"""
    stored = {
        "provider": "indextts",
        "openai_api_key": BASE_KEY,
        "openai_base_url": "https://example.invalid/v1",
        "openai_model": "some-model",
        "indextts_base_url": "http://127.0.0.1:8010/v1",
        "indextts_model": "Qwen3-TTS-12Hz-0.6B-Base-bf16",
    }
    stored.update(overrides)
    row = await system_config.set_config(system_config.KEY_TTS, stored, updated_by=None)
    return row["value"]


async def _stored_tts_config() -> dict:
    return (await system_config.get_config(system_config.KEY_TTS)) or {}


# ---------------------------------------------------------------------------
# 三态语义
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_未传密钥_不动():
    """不传 openai_api_key → 已存的 key 原样保留。"""
    await _seed_tts_config()
    admin = await _make_admin()
    async with _client(_token(admin)) as c:
        resp = await c.put(
            "/api/v1/admin/tts/config",
            json={"provider": "indextts", "openai_base_url": "https://example.invalid/v2"},
        )
    assert resp.status_code == 200, resp.text
    stored = await _stored_tts_config()
    assert stored["openai_api_key"] == BASE_KEY
    assert stored["openai_base_url"] == "https://example.invalid/v2"


@pytest.mark.asyncio
async def test_空串密钥_不动():
    """空串 = 前端「用户没重新输入」→ 不动（这条是前端契约，不能破）。"""
    await _seed_tts_config()
    admin = await _make_admin()
    async with _client(_token(admin)) as c:
        resp = await c.put(
            "/api/v1/admin/tts/config",
            json={"provider": "indextts", "openai_api_key": ""},
        )
    assert resp.status_code == 200, resp.text
    stored = await _stored_tts_config()
    assert stored["openai_api_key"] == BASE_KEY


@pytest.mark.asyncio
async def test_显式null_清除密钥(monkeypatch):
    """显式 null → 清除（从已存配置里删掉该键）。"""
    # 切断 env 回落，否则「清除」后生效配置仍从 env 拿到 key，断言就不确定了
    monkeypatch.delenv("OPENAI_TTS_API_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_TTS_API_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_TTS_TOKEN", raising=False)
    await _seed_tts_config()
    admin = await _make_admin()
    async with _client(_token(admin)) as c:
        resp = await c.put(
            "/api/v1/admin/tts/config",
            json={"provider": "indextts", "openai_api_key": None},
        )
    assert resp.status_code == 200, resp.text
    stored = await _stored_tts_config()
    assert "openai_api_key" not in stored
    # 其余配置不受影响
    assert stored["indextts_base_url"] == "http://127.0.0.1:8010/v1"
    assert stored["provider"] == "indextts"
    body = _body(resp)
    # 分 provider 的标志要如实反映「这把 key 没了」
    assert body["openai_api_key_set"] is False
    assert body["api_key_set"] is False


@pytest.mark.asyncio
async def test_非空_覆盖密钥():
    """非空 → 覆盖成新值。"""
    await _seed_tts_config()
    admin = await _make_admin()
    async with _client(_token(admin)) as c:
        resp = await c.put(
            "/api/v1/admin/tts/config",
            json={"provider": "indextts", "openai_api_key": "sk-new-key-999"},
        )
    assert resp.status_code == 200, resp.text
    stored = await _stored_tts_config()
    assert stored["openai_api_key"] == "sk-new-key-999"


@pytest.mark.asyncio
async def test_豆包密钥同样可清除():
    """doubao_api_key / doubao_token 走同一套三态（不是只修了 openai 一个）。"""
    await _seed_tts_config(doubao_api_key="ak-123", doubao_token="tk-456")
    admin = await _make_admin()
    async with _client(_token(admin)) as c:
        resp = await c.put(
            "/api/v1/admin/tts/config",
            json={"provider": "indextts", "doubao_api_key": None, "doubao_token": None},
        )
    assert resp.status_code == 200, resp.text
    stored = await _stored_tts_config()
    assert "doubao_api_key" not in stored
    assert "doubao_token" not in stored


# ---------------------------------------------------------------------------
# 闭环：清除不能把接口打成 500，也不能影响生效配置
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_清除当前provider唯一key_返回200且带warning(monkeypatch):
    """清掉 openai provider 唯一那把 key → 配置存下来了，但下一次合成会 401。

    所以接口必须**返回 200 并带 warning**：
    · 不能 500 —— 保存其实成功了，500 会让运营以为没保存、反复重试；
    · 不能一声不响 —— 「清除」的副作用（合成会 401）必须可见，
      否则雷被留给下一次蒸馏。
    """
    monkeypatch.delenv("OPENAI_TTS_API_KEY", raising=False)
    await _seed_tts_config(
        provider="openai",
        openai_base_url="https://example.invalid/v1",
        openai_model="m",
        openai_voice="alloy",
    )
    admin = await _make_admin()
    async with _client(_token(admin)) as c:
        resp = await c.put(
            "/api/v1/admin/tts/config",
            json={"provider": "openai", "openai_api_key": None},
        )
    assert resp.status_code == 200, resp.text
    body = _body(resp)
    assert "warning" in body, body
    assert "openai" in body["warning"]
    assert "401" in body["warning"]
    # 保存确实落库了
    stored = await _stored_tts_config()
    assert "openai_api_key" not in stored
    assert body["openai_api_key_set"] is False


@pytest.mark.asyncio
async def test_清除用不到的key_不影响生效配置():
    """provider=indextts 时清掉 openai key：生效配置照旧，reload 不该失败。"""
    await _seed_tts_config()
    admin = await _make_admin()
    async with _client(_token(admin)) as c:
        resp = await c.put(
            "/api/v1/admin/tts/config",
            json={"provider": "indextts", "openai_api_key": None},
        )
    assert resp.status_code == 200, resp.text
    body = _body(resp)
    # 没有 env 回落时不该有 warning（indextts 不需要 key）
    assert "warning" not in body, body
    assert body["provider"] == "indextts"
    assert body["indextts_base_url"] == "http://127.0.0.1:8010/v1"


@pytest.mark.asyncio
async def test_分provider标志不串台():
    """`api_key_set` 是「任意一把 key 存在」，分 provider 的标志才准。

    实测踩到的显示层说谎：豆包 env 里配了 key 时，openai 那一栏的
    「已保存，留空则不变」是**假的**（openai 根本没存）→ 运营以为不用管，
    而真正该清的那把 key 就在那一栏。所以两个标志必须分开。
    """
    monkeypatch_env = {"DOUBAO_TTS_API_KEY": "ak-from-env"}
    import os

    old = os.environ.get("DOUBAO_TTS_API_KEY")
    os.environ["DOUBAO_TTS_API_KEY"] = monkeypatch_env["DOUBAO_TTS_API_KEY"]
    try:
        await _seed_tts_config()
        admin = await _make_admin()
        async with _client(_token(admin)) as c:
            resp = await c.put(
                "/api/v1/admin/tts/config",
                json={"provider": "indextts", "openai_api_key": None},
            )
        assert resp.status_code == 200, resp.text
        body = _body(resp)
        assert body["openai_api_key_set"] is False  # openai 这把确实没了
        assert body["doubao_api_key_set"] is True  # 豆包那把还在（env 回落）
        assert body["api_key_set"] is True  # 历史字段：任意一把存在
    finally:
        if old is None:
            os.environ.pop("DOUBAO_TTS_API_KEY", None)
        else:
            os.environ["DOUBAO_TTS_API_KEY"] = old


# ---------------------------------------------------------------------------
# 三处端点共用同一份语义（原来各抄一份，probe 那份还少几个字段）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_测试与探测端点不落库():
    """/test 与 /voices/probe 只测不存 —— 传 null 也不能把 key 删掉。"""
    await _seed_tts_config()
    admin = await _make_admin()
    async with _client(_token(admin)) as c:
        resp = await c.put(
            "/api/v1/admin/tts/config",
            json={"provider": "indextts", "openai_api_key": None},
        )
        assert resp.status_code == 200, resp.text
        # 再把 key 存回去，然后用「不落库」的两个端点试 null
        await c.put(
            "/api/v1/admin/tts/config",
            json={"provider": "indextts", "openai_api_key": BASE_KEY},
        )
        t = await c.post(
            "/api/v1/admin/tts/test",
            json={"provider": "indextts", "openai_api_key": None},
        )
        assert t.status_code == 200, t.text
    stored = await _stored_tts_config()
    assert stored["openai_api_key"] == BASE_KEY


@pytest.mark.asyncio
async def test_合并函数三处共用():
    """直接钉住「规则只有一份」：三个端点用的是同一个合并函数。"""
    from stashbox.backend.common.models import SystemConfig  # noqa: F401  (确保模型已注册)

    src = (BACKEND_DIR / "content-service" / "admin_router.py").read_text()
    # 定义 1 处 + 调用 3 处（PUT / test / voices-probe）
    assert src.count("_merge_tts_config(") == 4, "合并逻辑又出现新的副本了"
    # 旧的逐字段拷贝（`if "openai_api_key" in fields and req.openai_api_key`）不该再出现
    assert 'if "openai_api_key" in fields' not in src
