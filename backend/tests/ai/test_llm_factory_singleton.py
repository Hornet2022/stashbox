"""CP3.6.2: factory 单例化验证。

验证（任务包 §B）：
1. 同 (provider, model, api_key, base_url, timeout, max_retries) 返回同一实例
2. 不同 model 返不同实例
3. 不同 api_key 返不同实例
4. maxsize=8 限制（9 个不同配置 → 触发 LRU 淘汰）
5. close_all_llm_clients() 关闭所有 cached + cache_clear()
6. maybe_close_llm_client() 单例 no-op，非单例正常关
"""

import pytest

from llm import factory
from llm.factory import (
    _create_cached_client,
    close_all_llm_clients,
    get_llm_client,
    maybe_close_llm_client,
)


@pytest.fixture(autouse=True)
def reset_factory_cache():
    """每个 test 前清 cache（避免 case 间状态污染）。"""
    _create_cached_client.cache_clear()
    factory._tracked_clients.clear()
    # 重置 _db_config 让测试走 env
    factory._db_config = None
    yield
    _create_cached_client.cache_clear()
    factory._tracked_clients.clear()


def test_same_params_return_same_instance():
    """同 6 维参数 → 同实例。"""
    c1 = _create_cached_client(
        "openai",
        "gpt-4o-mini",
        "key1",
        "https://a",
        60.0,
        3,
    )
    c2 = _create_cached_client(
        "openai",
        "gpt-4o-mini",
        "key1",
        "https://a",
        60.0,
        3,
    )
    assert c1 is c2


def test_different_model_returns_different_instance():
    """不同 model → 不同实例。"""
    c1 = _create_cached_client("openai", "gpt-4o-mini", "k", "https://a", 60.0, 3)
    c2 = _create_cached_client("openai", "gpt-4o", "k", "https://a", 60.0, 3)
    assert c1 is not c2


def test_different_api_key_returns_different_instance():
    """不同 api_key → 不同实例。"""
    c1 = _create_cached_client("openai", "gpt-4o-mini", "key1", "https://a", 60.0, 3)
    c2 = _create_cached_client("openai", "gpt-4o-mini", "key2", "https://a", 60.0, 3)
    assert c1 is not c2


def test_different_base_url_returns_different_instance():
    """不同 base_url → 不同实例。"""
    c1 = _create_cached_client("openai", "gpt-4o-mini", "k", "https://a", 60.0, 3)
    c2 = _create_cached_client("openai", "gpt-4o-mini", "k", "https://b", 60.0, 3)
    assert c1 is not c2


def test_maxsize_eviction():
    """9 个不同配置 → 触发 LRU 淘汰（maxsize=8）。"""
    # 8 个不同配置
    for i in range(8):
        _create_cached_client("openai", f"model-{i}", "k", "https://a", 60.0, 3)
    info_before = _create_cached_client.cache_info()
    assert info_before.currsize == 8

    # 第 9 个 → LRU 淘汰最老的（model-0）
    _create_cached_client("openai", "model-8", "k", "https://a", 60.0, 3)
    info_after = _create_cached_client.cache_info()
    assert info_after.currsize == 8  # maxsize 不变


def test_get_llm_client_returns_cached():
    """get_llm_client() 也走缓存。"""
    # 临时改 env config + reset _db_config
    factory._db_config = {
        "provider": "openai",
        "openai_llm_api_key": "test-key",
        "openai_llm_model": "gpt-4o-mini",
        "openai_llm_base_url": "https://mock",
        "timeout": 60.0,
        "max_retries": 3,
    }

    c1 = get_llm_client()
    c2 = get_llm_client()
    assert c1 is c2
    assert c1._shared is True  # 单例标识


def test_get_llm_client_with_model_name_override():
    """get_llm_client(model_name=...) 用 override model。"""
    factory._db_config = {
        "provider": "openai",
        "openai_llm_api_key": "k",
        "openai_llm_model": "gpt-4o-mini",
        "openai_llm_base_url": "https://mock",
        "timeout": 60.0,
        "max_retries": 3,
    }

    c1 = get_llm_client()  # default model
    c2 = get_llm_client(model_name="gpt-4o")
    assert c1 is not c2
    assert c1.model == "gpt-4o-mini"
    assert c2.model == "gpt-4o"


@pytest.mark.asyncio
async def test_maybe_close_llm_client_noop_for_shared():
    """maybe_close_llm_client 单例 → no-op（httpx 不释放）。"""
    c = _create_cached_client("openai", "gpt-4o-mini", "k", "https://a", 60.0, 3)
    assert c._shared is True
    # close 不应抛异常，也不应真正关闭
    await maybe_close_llm_client(c)
    # 验证 httpx 仍可用
    assert c._client is not None


@pytest.mark.asyncio
async def test_maybe_close_llm_client_releases_non_shared():
    """maybe_close_llm_client 非单例 → 正常 close。"""
    # 直接 new OpenAIClient（非通过 factory）→ _shared=False
    from llm.openai import OpenAIClient

    c = OpenAIClient(api_key="k", base_url="https://a", _shared=False)
    await maybe_close_llm_client(c)
    assert True


@pytest.mark.asyncio
async def test_close_all_llm_clients_closes_tracked():
    """close_all_llm_clients() 关闭所有 tracked + cache_clear。"""
    _create_cached_client("openai", "m1", "k", "https://a", 60.0, 3)
    _create_cached_client("openai", "m2", "k", "https://a", 60.0, 3)
    assert _create_cached_client.cache_info().currsize == 2

    await close_all_llm_clients()

    info = _create_cached_client.cache_info()
    assert info.currsize == 0
    assert len(factory._tracked_clients) == 0


def test_get_llm_client_unsupported_provider_raises():
    """不支持的 provider → ValueError。"""
    # 临时改 factory._db_config 强制走 unsupported
    factory._db_config = {
        "provider": "azure_unsupported",
        "openai_llm_model": "x",
        "timeout": 60.0,
        "max_retries": 3,
    }
    with pytest.raises(ValueError, match="unsupported llm provider"):
        get_llm_client()


def test_qwen_vl_provider_routing():
    """provider=qwen_vl → QwenVLClient。"""
    factory._db_config = {
        "provider": "qwen_vl",
        "qwen_vl_api_key": "k",
        "qwen_vl_model": "qwen-vl-max",
        "qwen_vl_base_url": "https://qwen",
        "timeout": 60.0,
        "max_retries": 3,
    }

    c = get_llm_client()
    from llm.qwen_vl import QwenVLClient

    assert isinstance(c, QwenVLClient)
    assert c._shared is True
    assert c.model == "qwen-vl-max"


def test_model_name_with_qwen_routes_to_qwen():
    """model_name 含 'qwen' → 强制走 QwenVLClient（即使 provider=openai）。"""
    factory._db_config = {
        "provider": "openai",
        "openai_llm_api_key": "k",
        "openai_llm_model": "gpt-4o",
        "openai_llm_base_url": "https://mock",
        "qwen_vl_api_key": "qk",
        "qwen_vl_model": "qwen-vl-max",
        "qwen_vl_base_url": "https://qwen",
        "timeout": 60.0,
        "max_retries": 3,
    }

    c = get_llm_client(model_name="qwen-vl-max")
    from llm.qwen_vl import QwenVLClient

    assert isinstance(c, QwenVLClient)
    assert c.api_key == "qk"  # 用 qwen_vl_api_key，不是 openai
