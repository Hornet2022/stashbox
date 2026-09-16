"""LLM Client 工厂测试（任务包 §4.3）。"""
import pytest
from config_llm import LLMSettings, llm_settings

from llm import get_claude_client, get_llm_client, get_qwen_vl_client
from llm.claude import ClaudeSonnetClient
from llm.mock import MockLLMClient
from llm.qwen_vl import QwenVLClient

CLAUDE_KEY = "sk-ant-test"
QWEN_KEY = "sk-dashscope-test"


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    """每个 case 都从「provider=mock + 空 key」开始，避免串味。"""
    monkeypatch.setattr(llm_settings, "llm_provider", "mock")
    monkeypatch.setattr(llm_settings, "claude_api_key", "")
    monkeypatch.setattr(llm_settings, "qwen_vl_api_key", "")


async def test_provider_mock_returns_mock_client():
    async with get_llm_client() as client:
        assert isinstance(client, MockLLMClient)
        assert type(client) is MockLLMClient


async def test_provider_claude_returns_claude_client():
    llm_settings.llm_provider = "claude"
    llm_settings.claude_api_key = CLAUDE_KEY

    async with get_llm_client() as client:
        assert isinstance(client, ClaudeSonnetClient)
        assert client.model == llm_settings.claude_model


async def test_provider_claude_without_api_key_raises():
    llm_settings.llm_provider = "claude"

    with pytest.raises(ValueError, match="api_key required"):
        get_llm_client()


async def test_provider_qwen_vl_returns_qwen_client():
    llm_settings.llm_provider = "qwen_vl"
    llm_settings.qwen_vl_api_key = QWEN_KEY

    async with get_llm_client() as client:
        assert isinstance(client, QwenVLClient)
        assert client.model == llm_settings.qwen_vl_model


async def test_provider_qwen_vl_without_api_key_raises():
    llm_settings.llm_provider = "qwen_vl"

    with pytest.raises(ValueError, match="api_key required"):
        get_llm_client()


async def test_model_name_routes_to_claude_even_when_provider_mock():
    """按 Step 选模型：显式传 claude-* 就走 Claude（v1 §5.2.3）。"""
    llm_settings.claude_api_key = CLAUDE_KEY
    model = "claude-4-sonnet-20250514"

    async with get_llm_client(model) as client:
        assert isinstance(client, ClaudeSonnetClient)
        assert client.model == model


async def test_model_name_routes_to_qwen_even_when_provider_mock():
    """显式传 qwen* 就走 Qwen VL（v1 §5.2.2 Step 1）。"""
    llm_settings.qwen_vl_api_key = QWEN_KEY
    model = "qwen2.5-vl-72b-instruct"

    async with get_llm_client(model) as client:
        assert isinstance(client, QwenVLClient)
        assert client.model == model


async def test_unknown_model_name_falls_back_to_mock():
    llm_settings.claude_api_key = CLAUDE_KEY

    async with get_llm_client("gpt-4o") as client:
        assert isinstance(client, MockLLMClient)


async def test_helper_get_claude_client():
    llm_settings.claude_api_key = CLAUDE_KEY

    async with await get_claude_client() as client:
        assert isinstance(client, ClaudeSonnetClient)
        assert client.api_key == CLAUDE_KEY


async def test_helper_get_qwen_vl_client():
    llm_settings.qwen_vl_api_key = QWEN_KEY

    async with await get_qwen_vl_client() as client:
        assert isinstance(client, QwenVLClient)
        assert client.api_key == QWEN_KEY


def test_llm_provider_env_override(monkeypatch):
    """CP3.5 切真 API 的开关：LLM_PROVIDER 环境变量。"""
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    assert LLMSettings().llm_provider == "claude"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-env")
    settings = LLMSettings()
    assert settings.claude_api_key == "sk-ant-env"
    assert settings.qwen_vl_api_key == "sk-dashscope-env"
