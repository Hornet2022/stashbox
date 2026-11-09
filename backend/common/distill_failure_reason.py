"""蒸馏失败的**面向用户**文案（backend/common：ai-service 与 content-service 共用）。

## 为什么要有这个模块

`articles.error` 原来直接写技术串：

    agent distill failed at rewrite (timeout): openai chat failed after 3 attempts: TimeoutError()

用户看到的是一串英文黑话 —— 既不知道"为什么失败"，也不知道"怎么办"。

**技术细节有它的去处**：journald（生产日志）+ `feedback.reason`（admin 面板）。
**用户界面要的是"为什么 + 怎么办"** —— 这就是本模块的职责。

放在 `common/` 而不是 `ai-service/`：写库的是 ai-service，读出来给 App 的是
content-service，两边都要用同一份定义（避免两边各写一份、口径漂移）。

## 文案原则

1. **说人话**：不出现 kind 名、HTTP 状态码、库名。
2. **给出下一步**：能不能重试、要不要等。
3. **不承诺我们不保证的事**：例如"额度已退还"——退款发生在**终局失败**时，
   中间态（还在重试）写这句话就是骗人。所以文案里不提退款。
"""

from __future__ import annotations

# 失败分类 → 用户看到的文案。
#
# kind 的取值来自 ai-service/agent/runner.py 的分类（_classify_llm_failure）
# 与 ai-service/llm/failure.py 的传输层分类。
KIND_MESSAGES: dict[str, str] = {
    # 上游连不上 / 上游已被熔断判定为不可用
    "upstream_unreachable": "AI 服务暂时连不上，我们正在等它恢复。请稍后重试。",
    "upstream_unavailable": "AI 服务暂时不可用，我们正在等它恢复。请稍后重试。",
    "connect": "AI 服务暂时连不上，请稍后重试。",
    # 上游还活着，只是这次没在时限内回话
    "timeout": "AI 服务这次响应超时了，请稍后重试。",
    # 传输层其它错误（连接被重置等）
    "network": "网络不太稳定，AI 服务没能完成这次处理，请稍后重试。",
    # 上游限流
    "ratelimit": "请求太密集了，稍等一会儿再试。",
    # 我方配置问题（对外不暴露细节）
    "auth": "AI 服务配置异常，我们正在修复，请稍后重试。",
    "notfound": "AI 服务暂时无法处理这类内容，请稍后重试。",
    "forbidden": "AI 服务暂时无法处理这类内容，请稍后重试。",
    # 熔断窗口外的其它内部错误
    "internal": "蒸馏过程中出了点问题，请稍后重试。",
    "badreq": "这篇内容暂时无法转成语音稿，请稍后重试。",
}

# 兜底文案（kind 缺失或未知）
DEFAULT_MESSAGE = "蒸馏失败了，请稍后重试。"


def user_failure_message(kind: str | None) -> str:
    """把机器可读的失败分类翻成**给用户看**的一句话。

    Args:
        kind: 失败分类（如 "upstream_unreachable" / "timeout"）。None 或未知
            走兜底文案。

    Returns:
        中文文案。**永远不返回空串** —— 空串会让客户端显示成"没有原因"，
        比一句笼统的话更糟。
    """
    if not kind:
        return DEFAULT_MESSAGE
    return KIND_MESSAGES.get(kind, DEFAULT_MESSAGE)
