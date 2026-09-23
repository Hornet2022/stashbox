"""LLM 配置（CP3.5-pre-1）。

独立于 backend/common/config.py 的 settings（本期不改 settings）——
LLM 相关配置只服务 ai-service 蒸馏链路，CP1.8+ 接 Nacos 动态配置时整体接管这里。

策略：全 OpenAI 协议栈（CP9.x 决策）。
- LLM：mock / openai / qwen_vl 三选一，全部走 OpenAI 兼容 HTTP + Bearer auth
- claude 原生（x-api-key + /v1/messages）已弃用，需要时改走 qwen_vl 或 openai 代理

CP3.5 接真 API 时通过环境变量注入：
- LLM_PROVIDER=openai                 # openai / qwen_vl（删 mock，默认走真实 LLM）
- QWEN_VL_BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
- DASHSCOPE_API_KEY=sk-...            # 别名：QWEN_VL_API_KEY（qwen_vl 专用）
- QWEN_VL_MODEL=qwen3.6-flash
- OPENAI_LLM_API_KEY=sk-...           # provider=openai 时的 LLM key
- OPENAI_LLM_MODEL=gpt-4o-mini        # provider=openai 时的模型
- OPENAI_LLM_BASE_URL=                # provider=openai 时可选（默认 OpenAI 官方）
- LLM_TIMEOUT=60
- LLM_MAX_RETRIES=3
"""

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # 全局 provider 路由：openai / qwen_vl（CP9.x 决策：删 claude，删 mock 回退）
    llm_provider: str = "openai"

    # —— Qwen VL（阿里 token-plan 团队版，OpenAI 兼容）——
    qwen_vl_api_key: str = Field(
        default="", validation_alias=AliasChoices("qwen_vl_api_key", "DASHSCOPE_API_KEY")
    )
    qwen_vl_base_url: str = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    qwen_vl_model: str = "qwen3.6-flash"

    # —— OpenAI 兼容 LLM（OpenAI 官方 / 任何 OpenAI-compatible 端点）——
    openai_llm_api_key: str = Field(default="", validation_alias="OPENAI_LLM_API_KEY")
    openai_llm_model: str = Field(default="gpt-4o-mini", validation_alias="OPENAI_LLM_MODEL")
    openai_llm_base_url: str = Field(
        default="https://api.openai.com/v1", validation_alias="OPENAI_LLM_BASE_URL"
    )

    # —— 通用 ——
    timeout: float = Field(default=60.0, validation_alias="LLM_TIMEOUT")
    max_retries: int = Field(default=3, validation_alias="LLM_MAX_RETRIES")


llm_settings = LLMSettings()
