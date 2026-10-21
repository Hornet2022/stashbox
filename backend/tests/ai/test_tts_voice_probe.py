"""TTS 音色探测（voice_probe）的解析逻辑 + admin 端点行为。

## 为什么测这个

运营配 TTS 最常踩的坑是 **voice 名不对**：第三方 OpenAI 兼容服务的
音色名跟 OpenAI 官方（alloy/echo/fable…）完全不一样。服务在 400 报错里
会把可用列表带出来（"Invalid voice 'x'. Supported: a, b, c"），
但那之前是埋在 detail 里，前端只显示"服务端处理异常"。

所以要保证：
1. 各种错误格式都能解析出音色列表
2. 解析不出来时**返回空列表**（不猜、不硬编码）
3. admin 端点真的把 supported_voices 带出去
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


def _load_voice_probe():
    spec = importlib.util.spec_from_file_location(
        "tts_voice_probe_under_test", BACKEND_DIR / "app/services/tts/voice_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


voice_probe = _load_voice_probe()
extract = voice_probe.extract_supported_voices


class TestExtractSupportedVoices:
    def test_invalid_voice_format(self):
        """实测那家服务的真实格式。"""
        text = (
            "Invalid voice 'alloy'. Supported: aiden, dylan, eric, ono_anna, "
            "ryan, serena, sohee, uncle_fu, vivian"
        )
        assert extract(text) == [
            "aiden",
            "dylan",
            "eric",
            "ono_anna",
            "ryan",
            "serena",
            "sohee",
            "uncle_fu",
            "vivian",
        ]

    def test_supported_voices_colon_format(self):
        assert extract("Supported voices: aiden, ryan") == ["aiden", "ryan"]

    def test_must_be_one_of_json_format(self):
        assert extract('"voice" must be one of ["alice", "bob"]') == ["alice", "bob"]

    def test_must_be_one_of_tuple_format(self):
        assert extract("voice must be one of ('x', 'y')") == ["x", "y"]

    def test_no_voice_info_returns_empty(self):
        """解析不出来就返回空 —— 不猜、不硬编码。"""
        assert extract("connection refused") == []
        assert extract("") == []
        assert extract(None) == []

    def test_strips_trailing_punctuation(self):
        assert extract("Supported: aiden, ryan.") == ["aiden", "ryan"]

    def test_deduplicates_preserving_order(self):
        assert extract("Supported: b, a, b") == ["b", "a"]

    def test_filters_junk_words(self):
        assert extract("Supported: aiden and ryan") == ["aiden", "ryan"]


class TestAdminProbeEndpoint:
    """admin_router 的探测端点：真的把 supported_voices 带出去。"""

    @pytest.fixture
    def probe_ep(self, monkeypatch):
        spec = importlib.util.spec_from_file_location(
            "cs_admin_router_probe", CONTENT_DIR / "admin_router.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        state: dict = {"db": {}, "raise_text": ""}

        async def _fake_get_config(key):
            return dict(state["db"]) or None

        monkeypatch.setattr(module.system_config, "get_config", _fake_get_config)
        monkeypatch.setenv("ENABLE_TTS_TEST_ENDPOINT", "1")

        # 桩掉 client：synthesize 抛带音色列表的错误
        class _C:
            provider_name = "openai"

            async def synthesize(self, text, voice=None, **kw):
                raise RuntimeError(
                    "OpenAITTS 错误 400: Invalid voice " f"'{voice}'. Supported: aiden, dylan, eric"
                )

            async def close(self):
                return None

        def _fake_build(cfg):
            return _C()

        monkeypatch.setattr(module, "build_client", _fake_build, raising=False)
        # build_client 是函数内 import 的，要打在模块属性上才生效 ——
        # 上面的 monkeypatch 若没打到，用 sys.modules 里的真模块补一刀。
        import stashbox.backend.app.services.tts as tts_mod

        monkeypatch.setattr(tts_mod, "build_client", _fake_build)

        async def _call(payload):
            req = module.TTSConfigUpdate(**payload)
            return await module.admin_tts_probe_voices(req, user={"id": 1})

        return _call, state

    @pytest.mark.asyncio
    async def test_probe_returns_supported_voices(self, probe_ep):
        call, _ = probe_ep
        result = await call({"provider": "openai", "openai_model": "m"})
        assert result["ok"] is True
        assert result["supported_voices"] == ["aiden", "dylan", "eric"]
        assert "3 个音色" in (result["hint"] or "")

    @pytest.mark.asyncio
    async def test_probe_rejects_unknown_provider(self, probe_ep):
        call, _ = probe_ep
        with pytest.raises(Exception) as exc:
            await call({"provider": "not-a-provider", "openai_model": "m"})
        assert "provider" in str(exc.value).lower()
