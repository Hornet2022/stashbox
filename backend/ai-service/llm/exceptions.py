"""LLM 自定义异常（CP3.5-pre-1）。"""


class LLMError(Exception):
    """LLM 通用错误。"""


class RateLimitError(LLMError):
    """触发速率限制（429）。"""

    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TokenLimitError(LLMError):
    """超过 max_tokens。"""
