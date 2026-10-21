"""LLM/TTS 测试端点的「空值沿用已存」语义（2026-10-21 修的真 bug）。

## 事故

运营在后台「LLM 设置」里配好了 provider / model / base_url / key，
点「测试调用」→ 报「服务端处理异常（响应格式非预期）」，
而同一个页面上另一个 GET 版的测试却是绿的。

## 根因

前端在用户**没有重新输入** key 时传的是 ``api_key: ""``（空串，不是省略），
意图是"用我已保存的那把"（前端源码里就是这么注释的）。

但 POST 版测试端点直接把 ``req.api_key`` 塞进 client 配置，
空串 → ``build_adhoc_client`` 抛 ``api_key required for OpenAIClient``
→ 端点 **4ms** 就返回 ok:false（根本没调 LLM）。

而 GET 版走的是已存配置，所以是绿的 —— 同一个页面两个按钮，
一个通一个不通。

TTS 那边的 POST 端点**一直是对的**（它用的是
``if "openai_api_key" in fields and req.openai_api_key:``，
空值不覆盖已存的），只有 LLM 侧漏了。

## 修法

POST 版对齐 PUT 保存端的合并语义：**空/不传 = 不动已存的值**，
依次回落 DB → env。

覆盖：
  1. api_key 为空串 → 沿用 DB 里的 key → 测试能通
  2. api_key 为空串且 DB 也没有 → 回落 env
  3. api_key 有值 → 用请求里的（不碰 DB）
  4. provider 非法 → 仍 400（校验不变）
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
def llm_test_ep(monkeypatch):
    """加载 content-service 的 admin_router，桩掉 DB 与 client 构造。

    返回 (调用函数, 状态字典)。状态字典里可以改 db 里的 key、
    env 里的 key，以及让 build_adhoc_client 记录它拿到的 key。
    """
    spec = importlib.util.spec_from_file_location(
        "cs_admin_router_llmtest", CONTENT_DIR / "admin_router.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    state: dict = {
        "db": {},  # system_config 里存的 llm 配置
        "env_key": "",  # env 里的 key
        "used_key": None,  # build_adhoc_client 实际拿到的 key
        "raise_on_empty": True,
    }

    async def _fake_get_config(key):
        return dict(state["db"]) or None

    monkeypatch.setattr(module.system_config, "get_config", _fake_get_config)

    def _fake_build(cfg):
        state["used_key"] = cfg.get("openai_llm_api_key")
        if state["raise_on_empty"] and not state["used_key"]:
            raise ValueError("api_key required for OpenAIClient")

        class _C:
            provider_name = "openai"

            async def chat(self, *a, **k):
                return "ok"

            async def aclose(self):
                return None

        return _C()

    monkeypatch.setattr(module, "build_adhoc_client", _fake_build)
    monkeypatch.setenv("OPENAI_LLM_API_KEY", state["env_key"])
    monkeypatch.setenv("ENABLE_LLM_TEST_ENDPOINT", "1")

    async def _call(payload):
        req = module.LLMTestRequest(**payload)
        return await module.admin_llm_test_with_config(req, user={"id": 1})

    return _call, state


@pytest.mark.asyncio
async def test_empty_api_key_falls_back_to_saved_key(llm_test_ep):
    """api_key 为空串 → 沿用 DB 里已存的那把 → 测试能通。"""
    call, state = llm_test_ep
    state["db"] = {"provider": "openai", "api_key": "sk-saved-in-db"}

    result = await call(
        {
            "provider": "openai",
            "model": "Qwen3.8-27B",
            "api_key": "",
            "base_url": "https://tertu.cn/v1",
        }
    )

    assert state["used_key"] == "sk-saved-in-db"
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_empty_api_key_falls_back_to_env(llm_test_ep, monkeypatch):
    """DB 也没有 key → 回落 env。"""
    call, state = llm_test_ep
    state["db"] = {}
    monkeypatch.setenv("OPENAI_LLM_API_KEY", "sk-from-env")

    result = await call(
        {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "",
            "base_url": "https://api.openai.com/v1",
        }
    )

    assert state["used_key"] == "sk-from-env"
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_provided_api_key_wins_over_saved(llm_test_ep):
    """请求里带了 key → 用请求里的（不碰已存的那把）。"""
    call, state = llm_test_ep
    state["db"] = {"provider": "openai", "api_key": "sk-saved-in-db"}

    await call(
        {
            "provider": "openai",
            "model": "Qwen3.8-27B",
            "api_key": "sk-fresh-from-form",
            "base_url": "https://tertu.cn/v1",
        }
    )

    assert state["used_key"] == "sk-fresh-from-form"


@pytest.mark.asyncio
async def test_rejects_unknown_provider(llm_test_ep):
    """非法 provider → 400（校验行为不变）。"""
    call, _ = llm_test_ep

    with pytest.raises(Exception) as exc:
        await call({"provider": "deepseek", "model": "x"})

    assert "provider" in str(exc.value).lower()
