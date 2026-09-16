"""content-service 单测 fixture（CP1.7）。

只放 fixture；非 fixture 的 helper 在 helpers.py（避免与 tests/conftest.py 混在一起）。
"""
import pytest

from helpers import FakeAIClient, content_main


@pytest.fixture(autouse=True)
def fake_ai_client(monkeypatch) -> FakeAIClient:
    """默认不真调 ai-service（单测环境没有 8103 在跑）。"""
    fake = FakeAIClient()
    monkeypatch.setattr(content_main, "get_ai_client", lambda: fake)
    return fake
