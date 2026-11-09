"""LLM 自定义异常（CP3.5-pre-1）。"""


class LLMError(Exception):
    """LLM 通用错误。

    `kind`：机器可读的失败分类（"unreachable" / "timeout" / "network" …）。
    存在的意义是**别再靠 str(e) 反推原因** —— str(exc) 常常是
    "TimeoutError()" 这种丢了根因的壳（见 llm/failure.py 的说明）。
    上层（agent 分类、用户文案）一律读这个字段。
    """

    kind: str | None = None


class RateLimitError(LLMError):
    """触发速率限制（429）。"""

    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TokenLimitError(LLMError):
    """超过 max_tokens。"""


class LLMCircuitOpen(LLMError):
    """熔断打开中：上游疑似不可用，**不要**再打它。

    带 `retry_after`（秒）—— 蒸馏层据此把 Arq 的退避拉长到熔断窗口那么久，
    否则固定 15s 的退避会在窗口内把重试次数全部烧光
    （5 次 × 15s = 75s，而窗口 120s —— 大半次重试都打在「拒绝服务」上）。

    形状照抄 TTS 侧的既有熔断器（app/services/tts/openai.py 的 `_CircuitOpen`），
    不另造一套并行体系。
    """

    kind = "upstream_unavailable"

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after
