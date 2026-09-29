"""CP-AGENT-TOOLS：听匣 agent 的 tool registry。

Tool 是 LangGraph agent 能调用的能力单元。每个 tool 接受 AgentState + args，
返回 (result_dict, side_effects)。Tool 由 ToolRegistry 统一管理，节点通过
registry.invoke(name, state, args) 调用。

4 个核心 tool（听匣业务最小集）：
  fetch_url           — 抓 URL 内容（mp.weixin.qq.com / douyin / pdf / generic_url）
  tts_synthesize      — TTS 合成（OpenAI 协议 / 火山方舟 / edge-tts / IndexTTS）
  stage_cache_lookup  — 查 pipeline stage 缓存（避免重复抓/重复改写）
  save_memory         — 写 user_profile / few_shot_example 到 PG（Phase 2 末）

设计原则：
  - tool 不直接写 LangGraph state —— 它只产 result + side_effects；节点决定
    哪些 side_effects 进 state。这样测试 tool 时不用 mock LangGraph。
  - tool 必须 idempotent + 失败显式 —— 失败抛 ToolError，节点 catch 后把
    kind/error 写进 state.error_kind。
  - tool 名字首字母暴露 --- 供 LangGraph tool_call schema 序列化。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("agent.tools")


class ToolError(Exception):
    """Tool 执行失败。节点应该 catch 后把 kind 写到 state.error_kind。"""

    def __init__(self, kind: str, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.kind = kind  # timeout / auth / notfound / connect / network / empty / internal
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    """自然语言描述：让 LangGraph / LLM 知道这个 tool 干什么。"""
    func: Callable[..., Awaitable[dict[str, Any]]]
    """async (state: AgentState, args: dict) -> dict（tool result）"""

    def to_openai_tool(self) -> dict[str, Any]:
        """暴露给 LLM 的 OpenAI tool schema（Phase 3 router 用）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                # 简化版 args schema：全部按 str 处理；真正的 schema 在 Phase 3 加
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }


# ---- 真实 tool 实现 --------------------------------------------------------


async def fetch_url_tool(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """CP-AGENT-TOOL-FETCH：抓 URL 内容（Phase 1 简化版走已有 fetcher）。

    真实生产实现：app/services/fetcher/* 里有多套 fetcher（wechat / douyin / pdf / generic）。
    这里只做：dispatcher.py 的 fetcher 决策 + 拿 articles_url 直转。

    返回：
      {content: str, meta: {title, author, word_count, published_at}, source_type: str}
    """
    url = state.get("url") or args.get("url")
    if not url:
        raise ToolError("badreq", "url 不能为空")
    source = state.get("source", "generic_url")

    # 简化实现：直接调 dispatcher
    try:
        # 延迟 import 避免循环
        from dispatcher import get_fetcher_for_url

        fetcher = get_fetcher_for_url(url)
        result = await fetcher.fetch(url)
        return {
            "content": result.get("content", ""),
            "meta": result.get("meta", {}),
            "source_type": result.get("source_type", source),
        }
    except Exception as exc:
        # 归类错误 → ToolError

        # OpenAIClient 这里不能直接复用，用更宽松的 _classify
        raise ToolError("network", f"fetch_url 失败: {exc}") from exc


async def tts_synthesize_tool(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """CP-AGENT-TOOL-TTS：TTS 合成。

    返回：
      {audio_path: str, audio_url: str|None, voice: str, duration_sec: int, bytes_len: int}

    CP-AGENT-TTS-PERSIST（实测修复）：
    旧实现返回 `getattr(client, "_last_audio_url", None)` —— 这个属性**根本不存在**，
    于是 audio_url 恒为 None。后果不是"拿不到 URL"这么轻：

        tts_node 写回 tts_audio_url=None
          → router 规则「有稿无音频 → skip_to_tts」
          → 回到 tts_node 再合成一次
          → 无限循环，每轮白合成一遍（实测一次任务合成了 3 遍、8MB+ 音频）

    现在改为**真实落盘**：音频写到 `INDEXTTS_AUDIO_DIR/{article_id}.{fmt}`，
    audio_path 一定有值；audio_url 只在 OSS 可用时才有值（当前
    `OSSStorage.save` 是空实现，会 NotImplementedError，所以这里是 None，
    distill_task 会记 `audio_url_not_http` 且不标 ready —— 这是既有约定，
    不要为了让它变绿而伪造 URL）。
    """
    script = state.get("rewritten_script") or args.get("script")
    voice = state.get("voice") or args.get("voice")
    if not script:
        raise ToolError("badreq", "script 不能为空（rewritten_script 字段未填）")

    from stashbox.backend.app.services.tts import reload as tts_reload

    client = await tts_reload()
    try:
        audio = await client.synthesize(script, voice=getattr(client, "voice", None))
    except Exception as exc:
        raise ToolError("internal", f"tts_synthesize 失败: {exc}") from exc

    if not audio or len(audio) < 100:
        raise ToolError("empty", f"TTS 返回空音频（{len(audio or b'')}B）")

    article_id = str(state.get("article_id") or "unknown")
    out_dir = Path(os.getenv("INDEXTTS_AUDIO_DIR", "data/audio")).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = "wav" if audio[:4] == b"RIFF" else "mp3"
    out_path = out_dir / f"{article_id}.{fmt}"
    out_path.write_bytes(audio)

    audio_url = await _try_upload_to_oss(article_id, audio, fmt)
    return {
        "audio_path": str(out_path),
        "audio_url": audio_url,
        "voice": voice or getattr(client, "voice", None),
        "duration_sec": _wav_duration_sec(audio) or len(audio) // 32000,
        "bytes_len": len(audio),
    }


async def _try_upload_to_oss(article_id: str, audio: bytes, fmt: str) -> str | None:
    """尝试上传 OSS；不可用时返回 None（**不要伪造 URL**）。

    当前 `app/services/storage/oss.py` 的 `OSSStorage.save` 是 NotImplementedError
    空实现，所以这里预期就是 None。上传可用后本函数会自动产出真实 URL。
    """
    try:
        from stashbox.backend.app.services.storage.oss import OSSStorage

        storage = OSSStorage()
        return await storage.save(f"audio/{article_id}.{fmt}", audio)
    except Exception as exc:
        log.info("agent_tts_oss_unavailable article_id=%s err=%s", article_id, exc)
        return None


def _wav_duration_sec(audio: bytes) -> int | None:
    """从 RIFF/WAV 头读时长（秒）；非 WAV 返回 None。"""
    try:
        import io
        import wave

        with wave.open(io.BytesIO(audio), "rb") as w:
            rate = w.getframerate()
            return int(w.getnframes() / rate) if rate else None
    except Exception:
        return None


async def stage_cache_lookup_tool(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """CP-AGENT-TOOL-STAGE-CACHE：查 pipeline stage 缓存。

    返回：
      {stage: str, hit: bool, data: dict | None}
    """
    from .stage_cache import read_stage

    stage = args.get("stage") or state.get("current_step", "fetch")
    article_id = state.get("article_id")
    if not article_id:
        raise ToolError("badreq", "article_id 不能为空")
    cached = await read_stage(article_id, stage)
    return {
        "stage": stage,
        "hit": cached is not None,
        "data": cached,
    }


async def save_memory_tool(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """CP-AGENT-TOOL-SAVE-MEMORY：写 memory 到 PG（Phase 2 末）。

    Phase 1 简化：占位实现，Phase 2 接 few_shot_examples 表。
    """
    return {
        "ok": True,
        "message": "save_memory tool 占位实现，Phase 2 接入 few_shot_examples 表",
    }


# ---- Tool registry ---------------------------------------------------------


class ToolRegistry:
    """CP-AGENT-TOOL-REGISTRY：统一管理 tool spec。

    用法：
      registry = ToolRegistry()
      registry.register(ToolSpec(name="fetch_url", ...))
      result = await registry.invoke("fetch_url", state, args={})
      specs = registry.list_specs()  # 给 LangGraph 工具调用决策用
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"tool {name!r} not registered (have: {list(self._tools)})") from None

    def list_specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    async def invoke(
        self, name: str, state: dict[str, Any], args: dict[str, Any]
    ) -> dict[str, Any]:
        spec = self.get(name)
        log.info(f"agent_tool_invoke name={name!r} args_keys={list(args.keys())}")
        try:
            result = await spec.func(state, args)
        except ToolError:
            raise  # 让节点 catch，写 state.error_kind
        except Exception as exc:
            raise ToolError("internal", f"{name} 未捕获异常: {exc}") from exc
        return result


# ---- 默认 registry（实例） -------------------------------------------------

# CP-AGENT-DEFAULT-REGISTRY-LAZY：模块 import 时不构造 ToolRegistry，避免
# register() 时把 func 引用捕获到 spec 对象里。这样测试 monkeypatch
# `agent.tools.fetch_url_tool` 时，下次 register() 会拿到新引用。
_default_registry_singleton: "ToolRegistry | None" = None


def get_default_registry() -> "ToolRegistry":
    """CP-AGENT-DEFAULT-REGISTRY：返回单例 ToolRegistry，每次首调时构造。

    返回的不是模块级常量而是函数调用结果，这样测试可以 monkeypatch 工具函数
    后再调一次 get_default_registry() 拿到新 registry（注册用 monkeypatched func）。

    生产代码：首次访问时构造并缓存；后续返回同一单例 —— 行为等价于模块级常量。
    """
    global _default_registry_singleton
    if _default_registry_singleton is None:
        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="fetch_url",
                description=(
                    "抓 URL 内容（mp.weixin.qq.com / douyin / pdf / 通用网页），"
                    "返回原文 markdown + 元信息（标题/作者/发布时间/字数）。"
                ),
                func=fetch_url_tool,
            )
        )
        reg.register(
            ToolSpec(
                name="tts_synthesize",
                description=(
                    "把听感稿合成语音，返回 OSS audio URL + 时长。"
                    "当前 TTS provider 由 admin 后台配置。"
                ),
                func=tts_synthesize_tool,
            )
        )
        reg.register(
            ToolSpec(
                name="stage_cache_lookup",
                description=(
                    "查 distill pipeline stage 缓存，避免重复抓/重复改写。"
                    "args.stage = 'fetch'|'rewrite'|'tts'|'concat'。"
                ),
                func=stage_cache_lookup_tool,
            )
        )
        reg.register(
            ToolSpec(
                name="save_memory",
                description=(
                    "写 user_profile 偏好 / few_shot_example 到 PG，跨任务记忆。"
                    "Phase 2 接 few_shot_examples 表。"
                ),
                func=save_memory_tool,
            )
        )
        _default_registry_singleton = reg
    return _default_registry_singleton


# 兼容老代码（runner.py 用过 `from agent.tools import default_registry`）。
# 通过 __getattr__ 在模块级别兜底返回 lazy 单例。
def __getattr__(name: str):
    """模块级懒加载：让老 `from agent.tools import default_registry` 仍能 work。"""
    if name == "default_registry":
        return get_default_registry()
    raise AttributeError(f"module 'agent.tools' has no attribute {name!r}")
