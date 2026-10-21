"""管理后台 LLM 配置端点（PUT /api/v1/admin/llm/config）的回归测试。

为什么单独一个文件（2026-10-21）
-------------------------------
这个端点之前**没有任何测试覆盖**，于是藏了一个真 bug：

    PUT 只能改 model（没顺手填 api_key）→ reload() 内部构造
    OpenAIClient 时抛 ValueError("api_key required for OpenAIClient")
    → 异常冒到框架 → **HTTP 500**

而实际上「保存」这个动作是成功的（DB 里已经写进去了）。
后果是运营在后台改个模型名就看到 500，弹一个看不懂的错误，
以为没保存 → 反复重试 → DB 里堆半截配置。

修复后：**保存照做**，缺 key 只是「还差一步」，用 warning 字段告知，
HTTP 状态码仍是 200。

覆盖：
  1. 缺 api_key → 200 + warning（不是 500）
  2. 带 api_key → 200 + 无 warning
  3. 非法 provider → 400（参数校验不变）
  4. 存的字段确实落到 system_config
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
CONTENT_DIR = BACKEND_DIR / "content-service"
if str(CONTENT_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_DIR))


@pytest.fixture
def llm_put(monkeypatch):
    """加载 content-service 的 admin_router，桩掉 DB 读写与 reload。

    返回一个可直接调用的 async 函数 + 一个「reload 会不会抛」的开关。
    """
    # 独立 import 一次 admin_router（content-service 的 main.py 会连带拉一堆依赖）
    spec = importlib.util.spec_from_file_location(
        "cs_admin_router_llmput", CONTENT_DIR / "admin_router.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    saved: dict = {}
    stored_value: dict = {}

    async def _fake_get_config(key):
        return dict(stored_value) or None

    async def _fake_set_config(key, value, updated_by=None):
        stored_value.clear()
        stored_value.update(value)
        saved["key"] = key
        saved["value"] = dict(value)
        saved["updated_by"] = updated_by
        return {"value": dict(value), "updated_at": None}

    monkeypatch.setattr(module.system_config, "get_config", _fake_get_config)
    monkeypatch.setattr(module.system_config, "set_config", _fake_set_config)

    # reload 的行为由测试控制：默认抛「缺 key」异常
    state = {"reload_raises": True}

    async def _fake_reload():
        if state["reload_raises"]:
            raise ValueError("api_key required for OpenAIClient")
        return type("C", (), {"provider_name": "openai"})()

    monkeypatch.setattr(module, "reload", _fake_reload)

    async def _call(payload):
        req = module.LLMConfigUpdate(**payload)
        return await module.admin_llm_config_put(req, user={"id": 1})

    return _call, state, saved, stored_value


@pytest.mark.asyncio
async def test_put_without_api_key_returns_200_with_warning(llm_put):
    """缺 api_key → 200 + warning（不是 500）。"""
    call, state, saved, _ = llm_put
    state["reload_raises"] = True

    result = await call({"provider": "openai", "model": "gpt-4o-mini"})

    assert result["provider"] == "openai"
    # 关键：warning 要说清「配置已保存，但立即生效失败」
    assert "warning" in result, "缺 key 时必须带 warning 字段提示运营"
    assert "api_key" in result["warning"]
    # 保存动作确实执行了
    assert saved["key"] == "llm"
    assert saved["value"]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_put_with_api_key_returns_200_no_warning(llm_put):
    """带 api_key → 200 且无 warning。"""
    call, state, saved, _ = llm_put
    state["reload_raises"] = False

    result = await call(
        {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "sk-test-1234567890",
        }
    )

    assert result["provider"] == "openai"
    assert "warning" not in result
    # key 不应该明文出网
    assert "sk-test-1234567890" not in str(result)
    assert result["api_key_set"] is True
    assert result["api_key_last4"] == "7890"
    assert saved["value"]["api_key"] == "sk-test-1234567890"


@pytest.mark.asyncio
async def test_put_rejects_unknown_provider(llm_put):
    """非法 provider → 400（参数校验行为不变）。"""
    call, _, saved, _ = llm_put

    with pytest.raises(Exception) as exc:
        await call({"provider": "deepseek", "model": "x"})

    assert "provider" in str(exc.value).lower()
    # 校验失败时不应该写库
    assert "key" not in saved
