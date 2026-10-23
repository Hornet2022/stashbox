"""admin TTS 配置：普通字段的合并语义（2026-10-23）。

## 为什么单独开一个文件

`tests/admin/test_admin_tts_config.py` 守的是**密钥字段的三态**（不传/空串/null）。
这里守的是**普通字段**的归一化 —— 它有个同型的坑：

普通字段里混着**数值型**（`indextts_temperature` / `indextts_top_p`），
而原实现对所有普通字段一律 `(v or "").strip() or None`：

  · float 没有 `.strip()` → **AttributeError**，整个保存请求 500
    （连同一次请求里的**别的字段**也一起存不进去）；
  · `0.0 or ""` → `""` → None —— temperature=0.0（贪心解码）被当成
    「没填」丢掉。

「空」只该指「没给」（None / 空串），**不是「等于零」**。

这些用例直接调纯函数 `_merge_tts_config`，不碰 DB —— 它是纯函数，
测它不需要数据库，也就不受环境是否有 PG 的影响。
"""

import importlib.util
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
CONTENT_DIR = BACKEND_DIR / "content-service"

# content-service 目录名带连字符，不能当包导入；它自己也要能 import
# `clients.ai_client`（admin_router.py 顶层就 import 了）。
if str(CONTENT_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_DIR))


def _load_admin_router():
    name = "_tts_cfg_fields_admin_router"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, CONTENT_DIR / "admin_router.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_router = _load_admin_router()
TTSConfigUpdate = _router.TTSConfigUpdate
_merge_tts_config = _router._merge_tts_config


def _merged(**kwargs) -> dict:
    return _merge_tts_config({}, TTSConfigUpdate(provider="openai", **kwargs))


def test_数值字段能正常保存():
    """temperature / top_p 是 float —— 一律 strip 会 AttributeError。"""
    m = _merged(indextts_temperature=0.7, indextts_top_p=0.9)
    assert m["indextts_temperature"] == 0.7
    assert m["indextts_top_p"] == 0.9


def test_零值不被当成没填():
    """temperature=0.0 是有意义的值（贪心解码），不能被 `or ""` 吞掉。"""
    m = _merged(indextts_temperature=0.0)
    assert m["indextts_temperature"] == 0.0
    assert m["indextts_temperature"] is not None


def test_字符串字段空串归一成_none():
    """空串 = 清空（回落 env），沿用既有语义。"""
    m = _merged(openai_instructions="", openai_voice="")
    assert m["openai_instructions"] is None
    assert m["openai_voice"] is None


def test_字符串字段会去掉首尾空白():
    m = _merged(openai_instructions="  用播音腔朗读  ")
    assert m["openai_instructions"] == "用播音腔朗读"


def test_风格控制_prompt_能存能清():
    m = _merged(openai_instructions="用平稳的播音腔朗读，语速适中。")
    assert m["openai_instructions"] == "用平稳的播音腔朗读，语速适中。"
    m2 = _merged(openai_instructions="")
    assert m2["openai_instructions"] is None


def test_不传的字段不进_merged():
    """不传 = 不动，merged 里不该凭空多出这个 key。"""
    m = _merged(openai_voice="uncle_fu")
    assert "openai_instructions" not in m
