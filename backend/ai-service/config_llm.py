"""LLM 配置（CP3.5-pre-1）。

独立于 backend/common/config.py 的 settings（本期不改 settings）——
LLM 相关配置只服务 ai-service 蒸馏链路，CP1.8+ 接 Nacos 动态配置时整体接管这里。

CP3.5 接真 API 时通过环境变量注入：
- LLM_PROVIDER=claude
- ANTHROPIC_API_KEY=sk-ant-...
- DASHSCOPE_API_KEY=sk-...
- CLAUDE_MODEL=claude-4-sonnet-20250514
- QWEN_VL_MODEL=qwen2.5-vl-72b-instruct
"""
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # 默认 mock，CP3.5 切到 claude / qwen_vl
    llm_provider: str = "mock"  # mock / claude / qwen_vl

    # Claude（v1 §5.2.3 Step 2 听感改写 + §5.4 Step 3 主题标签）
    # ANTHROPIC_API_KEY 是官方 SDK 的惯用名，和 CLAUDE_API_KEY 都收
    claude_api_key: str = Field(
        default="", validation_alias=AliasChoices("claude_api_key", "ANTHROPIC_API_KEY")
    )
    claude_model: str = "claude-4-sonnet-20250514"

    # Qwen VL（v1 §5.2.2 Step 1 内容结构化）
    # DASHSCOPE_API_KEY 是阿里云 DashScope 的惯用名
    qwen_vl_api_key: str = Field(
        default="", validation_alias=AliasChoices("qwen_vl_api_key", "DASHSCOPE_API_KEY")
    )
    qwen_vl_model: str = "qwen2.5-vl-72b-instruct"

    # 通用
    timeout: float = 60.0
    max_retries: int = 3


llm_settings = LLMSettings()
