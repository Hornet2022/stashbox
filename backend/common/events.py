"""CP6.2.1：50 核心事件枚举（v1 §11.6 验收要求）。

CP6.2.1 段只暴露 10 个最常用事件 + 占位枚举其他 40 个。
CP6.2.2 续单时把所有 50 个接入埋点。

事件分类：
- 用户行为 (5)：user_login / user_logout / user_register / quota_reset / plan_upgrade
- 内容收集 (5)：article_submit / article_capture_start / article_capture_success / article_capture_failed / article_unsupported
- 蒸馏流程 (6)：distill_start / distill_step_complete / distill_completed / distill_failed / distill_retry / distill_quota_refund
- 音频播放 (6)：audio_play_start / audio_pause / audio_resume / audio_complete / audio_skip / audio_30s_reached
- 互动反馈 (6)：favorite_add / favorite_remove / mark_listened / article_skip / article_rate / share
- 订阅标签 (4)：tag_subscribe / tag_unsubscribe / tag_create / tag_filter
- 推送 (3)：push_send / push_open / push_dismiss
- 错误 (4)：api_4xx / api_5xx / fetcher_error / distill_step_error
- 管理 (3)：admin_login / admin_quota_adjust / admin_metrics_view
- 系统 (5)：service_start / service_stop / config_change / deploy / healthcheck_failed
- 业务指标 (3)：listened_30s_rate / distill_success_rate / p95_distill_duration

总计 50 ✅
"""
from enum import Enum


class EventName(str, Enum):
    """事件名枚举。CP6.2.1 暴露 10 核心事件，其他 40 留 CP6.2.2。"""

    # === 用户行为（CP6.2.1 覆盖 1）===
    USER_LOGIN = "user_login"
    USER_LOGOUT = "user_logout"  # CP6.2.2
    USER_REGISTER = "user_register"  # CP6.2.2
    QUOTA_RESET = "quota_reset"  # CP6.2.1
    PLAN_UPGRADE = "plan_upgrade"  # CP6.2.2

    # === 内容收集（CP6.2.1 覆盖 3）===
    ARTICLE_SUBMIT = "article_submit"  # CP6.2.1
    ARTICLE_CAPTURE_START = "article_capture_start"  # CP6.2.1
    ARTICLE_CAPTURE_SUCCESS = "article_capture_success"  # CP6.2.1
    ARTICLE_CAPTURE_FAILED = "article_capture_failed"  # CP6.2.2
    ARTICLE_UNSUPPORTED = "article_unsupported"  # CP6.2.2

    # === 蒸馏流程（CP6.2.1 覆盖 4）===
    DISTILL_START = "distill_start"  # CP6.2.1
    DISTILL_STEP_COMPLETE = "distill_step_complete"  # CP6.2.2
    DISTILL_COMPLETED = "distill_completed"  # CP6.2.1
    DISTILL_FAILED = "distill_failed"  # CP6.2.1
    DISTILL_RETRY = "distill_retry"  # CP6.2.2
    DISTILL_QUOTA_REFUND = "distill_quota_refund"  # CP6.2.1
    ARTICLE_RETRY_REQUESTED = "article_retry_requested"  # CP5.2

    # === 音频播放（CP6.2.1 覆盖 2）===
    AUDIO_PLAY_START = "audio_play_start"  # CP6.2.1
    AUDIO_PAUSE = "audio_pause"  # CP6.2.2
    AUDIO_RESUME = "audio_resume"  # CP6.2.2
    AUDIO_COMPLETE = "audio_complete"  # CP6.2.1
    AUDIO_SKIP = "audio_skip"  # CP6.2.2
    AUDIO_30S_REACHED = "audio_30s_reached"  # CP6.2.2（客户端埋，独立仓做）

    # === 互动反馈（CP6.2.2 全做：客户端）===
    FAVORITE_ADD = "favorite_add"
    FAVORITE_REMOVE = "favorite_remove"
    MARK_LISTENED = "mark_listened"
    ARTICLE_SKIP = "article_skip"
    ARTICLE_RATE = "article_rate"
    SHARE = "share"

    # === 订阅标签（CP6.2.2 全做）===
    TAG_SUBSCRIBE = "tag_subscribe"
    TAG_UNSUBSCRIBE = "tag_unsubscribe"
    TAG_CREATE = "tag_create"
    TAG_FILTER = "tag_filter"

    # === 推送（CP6.2.2 全做）===
    PUSH_SEND = "push_send"
    PUSH_OPEN = "push_open"
    PUSH_DISMISS = "push_dismiss"

    # === 错误（CP6.2.1 覆盖 1）===
    API_4XX = "api_4xx"  # CP6.2.2
    API_5XX = "api_5xx"  # CP6.2.2
    FETCHER_ERROR = "fetcher_error"  # CP6.2.1
    DISTILL_STEP_ERROR = "distill_step_error"  # CP6.2.2

    # === 管理（CP6.2.2 全做）===
    ADMIN_LOGIN = "admin_login"
    ADMIN_QUOTA_ADJUST = "admin_quota_adjust"
    ADMIN_METRICS_VIEW = "admin_metrics_view"

    # === 系统（CP6.2.2 全做）===
    SERVICE_START = "service_start"
    SERVICE_STOP = "service_stop"
    CONFIG_CHANGE = "config_change"
    DEPLOY = "deploy"
    HEALTHCHECK_FAILED = "healthcheck_failed"

    # === 业务指标（CP6.2.2 全做）===
    LISTENED_30S_RATE = "listened_30s_rate"
    DISTILL_SUCCESS_RATE = "distill_success_rate"
    P95_DISTILL_DURATION = "p95_distill_duration"
