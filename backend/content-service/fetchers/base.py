"""抓取核心抽象层（v1 §11.2 CP2.1）。

只定义契约：Fetcher ABC / FetchResult / FetcherError / FetcherErrorCode。
真抓取实现留给 CP2.2（公众号）/ CP2.3（抖音）/ CP2.4（通用 URL）。

注意：FetcherErrorCode 是 fetcher 私有的业务错误码，**不**并入 v1 §3.x 的 biz_code 体系，
对外暴露时由上层（CP2.5 服务号 Handler）再映射成统一错误响应。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class FetcherErrorCode(str, Enum):
    """fetcher 私有业务错误码（不对齐 v1 §3.x biz_code）。"""

    NETWORK = "fetcher.network"  # 网络层失败
    PARSE = "fetcher.parse"  # HTML/JSON 解析失败
    AUTH = "fetcher.auth"  # 需要登录/cookie
    RATE_LIMIT = "fetcher.rate_limit"  # 被目标站限速
    NOT_FOUND = "fetcher.not_found"  # 文章已被删除/404
    UNSUPPORTED = "fetcher.unsupported"  # URL 不被本 fetcher 支持
    INTERNAL = "fetcher.internal"  # 内部错误

    def __str__(self) -> str:
        return self.value


class FetcherError(Exception):
    """抓取失败的业务异常。

    格式：`[source] code: message`，例如 `[wechat_mp] fetcher.network: timeout`。
    """

    def __init__(self, code: FetcherErrorCode, message: str, *, source: str):
        self.code = code
        self.message = message
        self.source = source  # "wechat_mp" / "douyin" / "generic_url"
        super().__init__(f"[{source}] {code}: {message}")


@dataclass
class FetchResult:
    """抓取结果（统一格式，所有 Fetcher 都返回这个）。"""

    url: str  # 原始 URL
    title: str  # 文章标题
    content_html: str  # 清洗后的 HTML（CP2 不存原始 HTML）
    content_text: str  # 纯文本（用于 Step 1 多模态理解）
    author: str | None = None  # 作者
    publish_time: datetime | None = None  # 发布时间（带时区）
    media_urls: list[str] = field(default_factory=list)  # 图片/视频 URL 列表
    source: str = "unknown"  # "wechat_mp" / "douyin" / "generic_url"
    raw_metadata: dict = field(default_factory=dict)  # 原始元数据（CP2 调试用）


class Fetcher(ABC):
    """抓取器抽象基类（v1 §11.2 CP2.1）。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Fetcher 名字（如 'wechat_mp' / 'douyin' / 'generic_url'）。"""
        ...

    @abstractmethod
    def supports(self, url: str) -> bool:
        """判断本 fetcher 能否处理这个 URL（URL pattern 匹配）。

        CP2.2 微信公众号：mp.weixin.qq.com / 二维码长按场景
        CP2.3 抖音：douyin.com / v.douyin.com / iesdouyin.com
        CP2.4 通用：catch-all（任何 URL 都能匹配，但优先级最低）
        """
        ...

    @abstractmethod
    async def fetch(self, url: str, *, timeout: float = 30.0) -> FetchResult:
        """真正抓取（CP2.2-CP2.4 实现，本期 raise FetcherError 占位）。

        Args:
            url: 目标 URL
            timeout: 超时秒数（默认 30s，CP2.7 集成测可调）

        Returns:
            FetchResult：统一格式

        Raises:
            FetcherError：失败时抛业务异常
        """
        ...
