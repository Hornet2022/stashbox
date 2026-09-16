"""content-service Pydantic schemas（CP1.7）。

从 main.py 抽出，路由只做编排，模型定义集中在此。
"""
from pydantic import BaseModel


class AddArticleRequest(BaseModel):
    url: str
    source: str = "web"  # wechat | douyin | web | pdf | d9 | clawbot


class ArticleResponse(BaseModel):
    id: str
    url: str
    source: str
    title: str | None = None
    owner_id: str
    status: str  # pending | distilling | ready | listened | failed
    favorite: bool
    skip: bool
    created_at: str


class D9AddRequest(BaseModel):
    """D9 入口请求（v1 §3.5）：微信「更多打开方式」共享过来的链接。"""

    url: str
    title: str | None = None
    source: str = "wechat"


class D9AddResponse(BaseModel):
    article_id: str
    task_id: str | None = None  # 蒸馏任务；触发 ai-service 失败时为 None
    status: str  # pending | distilling
    estimated_distill_seconds: int = 30
    device_id: str | None = None  # 匿名时回显


class ArticleStatusResponse(BaseModel):
    """文章 + 蒸馏任务聚合状态（articles LEFT JOIN distilled_articles）。"""

    article_id: str
    status: str  # pending | distilling | ready | failed | listened
    task_id: str | None = None
    task_status: str | None = None  # queued | running | done | failed
    error: str | None = None
    audio_url: str | None = None
    audio_duration_sec: int | None = None
    tags: list[str] | None = None
    quality_score: float | None = None
    created_at: str
    updated_at: str


class AudioUrlResponse(BaseModel):
    article_id: str
    audio_url: str  # OSS 签名 URL（本期 mock，CP1.8+ 接真签名）
    expires_at: str  # ISO 8601
    duration_sec: int


class ClawBotMessageRequest(BaseModel):
    text: str
    user_id: str | None = None


class WechatMpMessageRequest(BaseModel):
    """微信公众号服务号回调消息（v1 §11.2 CP2.5）。"""

    from_user: str  # 公众号 openid（本期不用，纯接收）
    text: str  # 用户发的文本（可能含 URL）
    create_time: int  # 消息时间戳
    msg_id: str | None = None  # 消息 ID（幂等用，本期不实现）
