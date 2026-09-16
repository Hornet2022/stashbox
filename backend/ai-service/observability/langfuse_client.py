"""Langfuse 客户端封装（CP3.5-pre-4，v1 §10.6 蒸馏监控）。

默认关闭：`LANGFUSE_ENABLED` 未设或不等于 true 时，所有方法返 None，
既不 import langfuse SDK 也不发网络请求 —— dev / CI / 单测零副作用。

启用（env 三件套配齐才上报）：
    LANGFUSE_ENABLED=true
    LANGFUSE_PUBLIC_KEY=pk-lf-xxx
    LANGFUSE_SECRET_KEY=sk-lf-xxx
    LANGFUSE_HOST=https://cloud.langfuse.com  # 可选

注意：任务包 §3.1 用的是 langfuse v2 API（client.trace()/span()/generation()），
所以依赖锁在 `langfuse>=2.57,<3`（v3/v4 改成了 start_observation，不是这套签名）。
"""
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://cloud.langfuse.com"


class LangfuseClient:
    """Langfuse 客户端封装（单例，可选启用）。

    上报失败只在日志里留痕，不往上抛 —— 观测链路不能把蒸馏主流程搞挂（任务包 §8）。
    """

    _instance: "LangfuseClient | None" = None

    def __init__(self):
        self.enabled = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"
        self._client = None

        if not self.enabled:
            return

        try:
            from langfuse import Langfuse
        except ImportError:
            logger.warning("LANGFUSE_ENABLED=true 但 langfuse SDK 没装，降级为不上报")
            self.enabled = False
            return

        try:
            self._client = Langfuse(
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
                secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
                host=os.getenv("LANGFUSE_HOST", DEFAULT_HOST),
            )
        except Exception as exc:  # 构造失败（网络 / key 校验）不冒泡
            logger.warning("langfuse 初始化失败，降级为不上报: %s", exc)
            self.enabled = False
            self._client = None

    # ------------------------------------------------------------------
    # 单例
    # ------------------------------------------------------------------
    @classmethod
    def get(cls) -> "LangfuseClient":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """清单例（单测换 env 用）。"""
        cls._instance = None

    # ------------------------------------------------------------------
    # 上报
    # ------------------------------------------------------------------
    def create_trace(self, name: str, metadata: dict | None = None, **kwargs) -> Any:
        """创建顶层 trace；disable 模式返 None。"""
        if not self.enabled:
            return None
        try:
            return self._client.trace(name=name, metadata=metadata or {}, **kwargs)
        except Exception as exc:
            logger.warning("langfuse create_trace 失败（忽略）: %s", exc)
            return None

    def create_span(self, trace: Any, name: str, input: Any = None, **kwargs) -> Any:
        """在 trace 下创建 span（单步调用）。trace 为空 / disable 时返 None。"""
        if not self.enabled or trace is None:
            return None
        try:
            return trace.span(name=name, input=input, **kwargs)
        except Exception as exc:
            logger.warning("langfuse create_span 失败（忽略）: %s", exc)
            return None

    def create_generation(
        self,
        span: Any,
        name: str,
        model: str,
        input: Any = None,
        output: Any = None,
        usage: dict | None = None,
        **kwargs,
    ) -> Any:
        """记录 LLM 调用（model + usage）。span 为空 / disable 时返 None。"""
        if not self.enabled or span is None:
            return None
        try:
            return span.generation(
                name=name,
                model=model,
                input=input,
                output=output,
                usage=usage or {},
                **kwargs,
            )
        except Exception as exc:
            logger.warning("langfuse create_generation 失败（忽略）: %s", exc)
            return None
