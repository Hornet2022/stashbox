"""
配置管理 - 基于 pydantic-settings 支持多环境。
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置（4 服务共享）"""

    model_config = SettingsConfigDict(
        env_file=".env",
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
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7

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
