"""
配置管理 - 基于 pydantic-settings 支持多环境。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# CP-ENV-CWD：把 backend/.env 按**绝对路径**灌进 os.environ，且只做一次。
#
# 为什么必须显式 load_dotenv：SettingsConfigDict(env_file=".env") 是**相对 CWD**
# 解析的，而各进程的 CWD 并不统一 ——
#   - api-gateway / content-service / user-service 的 LaunchAgent CWD 是 backend/
#   - **ai-service 和 ai-worker 的 CWD 是 backend/ai-service/**
# 后者去找 backend/ai-service/.env（不存在）→ 蒸馏链路**整条都读不到 .env**：
# INDEXTTS_REF_AUDIO 读不到就直接抛「IndexTTS 需要参考音频」，
# INDEXTTS_BASE_URL 落回 DEFAULT，LLM/TTS 全部走代码默认值。
#
# 为什么不能只改 env_file 路径：pydantic-settings 只把 .env 灌进 Settings 对象，
# **不会写进 os.environ**。而 app/services/llm/__init__.py 等模块是直接
# `os.getenv("OPENAI_LLM_API_KEY")` 读的 —— 那些调用点只有 os.environ 里有值。
# 所以这里显式 load_dotenv，两种读法都覆盖到。
#
# override=False：真实环境变量（launchd plist 里注入的）优先级高于 .env，不要被文件盖掉。
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if _ENV_FILE.is_file():
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_FILE, override=False)
    except ImportError:  # 没装 python-dotenv 时退回 pydantic-settings 的 env_file
        pass


class Settings(BaseSettings):
    """全局配置（4 服务共享）"""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "stashbox"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    debug: bool = True

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "stashbox"
    postgres_password: str = "stashbox_dev"
    postgres_db: str = "stashbox"
    postgres_pool_size: int = 10
    postgres_pool_recycle: int = 3600

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""

    jwt_secret: str = "dev-secret-change-me-in-production"

    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, v: str) -> str:
        """启动断言：禁止用默认 dev 密钥（防止生产环境误用，任何人能伪造 token）。

        本地 dev 可通过环境变量 STASHBOX_ALLOW_DEV_JWT=1 显式放行（CI / 集成测试用）。
        """
        if v == "dev-secret-change-me-in-production":
            import os

            if not os.getenv("STASHBOX_ALLOW_DEV_JWT"):
                raise ValueError(
                    "jwt_secret 仍为默认值。生产必须通过环境变量 JWT_SECRET 注入强密钥；"
                    "本地 dev 可设 STASHBOX_ALLOW_DEV_JWT=1 显式放行。"
                )
        if len(v) < 16:
            raise ValueError(f"jwt_secret 太短（{len(v)} 字符 < 16），建议至少 32 字符的随机串。")
        return v

    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7
    jwt_refresh_expire_minutes: int = 60 * 24 * 30  # refresh token 有效期 30 天（rotate 用）

    user_service_url: str = "http://user-service:8001"
    content_service_url: str = "http://content-service:8002"
    ai_service_url: str = "http://ai-service:8003"

    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    claude_api_key: str = ""
    claude_base_url: str = "https://api.anthropic.com"
    doubao_tts_app_id: str = ""
    doubao_tts_token: str = ""

    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""

    # CP7.3.5: CORS 白名单（逗号分隔）。原来是 content-service 没装 CORS 中间件，
    # admin-web 只能靠 vite proxy 绕过；生产没 proxy，这里给出 env 可覆盖的默认：
    #   CORS_ORIGINS="https://admin.example.com,https://admin2.example.com"
    cors_origins: str = "http://localhost:3000,http://localhost:5174"

    # CP9.x dev 兜底：本地音频静态挂载（gateway 把 /audio/* 挂到 LOCAL_AUDIO_DIR）
    # 与 api-gateway/main.py 里的 ENABLE_LOCAL_AUDIO_MOUNT 一起用：
    #   STORAGE_PROVIDER=local + ENABLE_LOCAL_AUDIO_MOUNT=1 + LOCAL_AUDIO_DIR=/tmp/audio
    # 真机客户端拿到 audio_url 时不能是 localhost（设备本机 = 自己），
    # 这里 PUBLIC_GATEWAY_URL 给出对外可达的 gateway 前缀。
    storage_provider: str = "oss"
    enable_local_audio_mount: bool = False
    local_audio_dir: str = "/tmp/audio"
    public_gateway_url: str = "http://localhost:8100"

    @property
    def cors_origins_list(self) -> list[str]:
        """逗号分隔 → list。配了 "*" 表示放行全部 origin（dev 兜底）。"""
        items = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return items or ["*"]

    @property
    def database_url(self) -> str:
        host = self.postgres_host
        port = str(self.postgres_port)
        user = self.postgres_user
        pw = self.postgres_password
        db = self.postgres_db
        return "postgresql+asyncpg://" + user + ":" + pw + "@" + host + ":" + port + "/" + db

    @property
    def redis_url(self) -> str:
        auth = ":" + self.redis_password + "@" if self.redis_password else ""
        host = self.redis_host
        port = str(self.redis_port)
        db = str(self.redis_db)
        return "redis://" + auth + host + ":" + port + "/" + db


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
