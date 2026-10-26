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
    # 2026-10-21：用户主动注销账号（合规要求可追溯"发生过注销"这件事，
    # 但不能留下个人内容 —— 埋点只带 user_id，不带任何个人信息）
    USER_ACCOUNT_DELETED = "user_account_deleted"
    # 2026-10-21：用户同意 / 撤回隐私政策
    USER_CONSENT_UPDATED = "user_consent_updated"

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
    #
    # ⚠️ AUDIO_PLAY_START 与 AUDIO_PLAY_START_CLIENT 是**两件不同的事**，
    # 别合并（2026-10-25 实测踩过）：
    #
    #   AUDIO_PLAY_START        —— **服务端**打的：用户点了播放 → 前端来取音频
    #                               直链（content-service/main.py 的 audio-url
    #                               端点）。它是「取到地址了」，不是「声音出来了」。
    #   AUDIO_PLAY_START_CLIENT —— **客户端**打的：用户点了播放 → 播放器真的
    #                               进入出声状态（ExoPlayer isPlaying 由 false
    #                               变 true）。它带 duration_ms。
    #
    # 为什么必须是两个事件名：A/B 报表（ai-service/distill/ab_report.py）直接
    # `count(Feedback.type == 'audio_play_start')` 当**播放次数**的分母 ——
    # 完听率 = audio_complete / audio_play_start，复听率 = 同一 (user, article)
    # 有 ≥2 次播放。客户端再用同名打一次 ⇒ 一次播放变两条 ⇒
    # **完听率直接掉一半、复听率把「播放 1 次」误判成「复听过」**。
    #
    # 也不改服务端那条的名字：那样会断 A/B 报表的历史序列（0029 迁移上线前的
    # 数据都是 audio_play_start），而客户端这条是**新指标**（只为带 duration_ms），
    # 本来就不该混进「播放次数」的分母。
    AUDIO_PLAY_START = "audio_play_start"  # CP6.2.1（服务端：取到音频直链）
    AUDIO_PLAY_START_CLIENT = "audio_play_start_client"  # 客户端：真的出声了
    AUDIO_PAUSE = "audio_pause"  # CP6.2.2
    AUDIO_RESUME = "audio_resume"  # CP6.2.2
    AUDIO_COMPLETE = "audio_complete"  # CP6.2.1
    AUDIO_SKIP = "audio_skip"  # CP6.2.2
    AUDIO_30S_REACHED = "audio_30s_reached"  # CP6.2.2（客户端埋，独立仓做）

    # === 客户端性能（2026-10-25 补）===
    #
    # 「7 大指标」里两个**客户端**指标的落点（cp10.9-product-audit.md §4）：
    #   · 首屏加载 < 1.5s  → APP_FIRST_FRAME，properties.duration_ms
    #   · 音频启动 < 500ms → AUDIO_PLAY_START_CLIENT，properties.duration_ms
    #
    # 为什么音频启动**不**复用服务端的 AUDIO_PLAY_START：见上面那段说明 ——
    # 那两个事件测的是**两件不同的事**（取到直链 vs 真的出声），复用会污染
    # A/B 报表的播放次数分母。
    APP_FIRST_FRAME = "app_first_frame"

    # === 播放中断与恢复（2026-10-26 补）===
    #
    # 内测反馈「放后台播放，几分钟后就没声了」——但此前播放失败**只写进
    # 客户端的 logcat**，服务端一个字都没留 ⇒ 只能靠猜是哪一类断流。
    #
    # 这两条是一对：
    #   · PLAYBACK_ERROR —— 断了。properties: kind / attempt /
    #     position_ms / duration_ms / cause / article_id
    #   · PLAYBACK_RECOVERED —— 自己接上了。properties: attempts / article_id
    #
    # 判读方式：**只有失败没有恢复** ⇒ 彻底断了（音频没了 / 网络彻底不通）；
    # **成对出现** ⇒ 抖了一下，已自动接上（此时该看的是「抖动频率」，
    # 而不是「播放成功率」）。
    PLAYBACK_ERROR = "playback_error"
    PLAYBACK_RECOVERED = "playback_recovered"

    # === 互动反馈（CP6.2.2 全做：客户端）===
    FAVORITE_ADD = "favorite_add"
    FAVORITE_REMOVE = "favorite_remove"
    MARK_LISTENED = "mark_listened"
    ARTICLE_SKIP = "article_skip"
    ARTICLE_RATE = "article_rate"
    SHARE = "share"
    ARTICLE_SNOOZE = "article_snooze"  # CP5.5
    FEEDBACK_V2_SUBMIT = "feedback_v2_submit"  # CP5.5-A3

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

    # === 引导流程（CP5.1）===
    ONBOARDING_STARTED = "onboarding_started"
    ONBOARDING_STEP_VIEWED = "onboarding_step_viewed"  # 3 步引导：复制链接 / 打开 D9 / 听音频
    ONBOARDING_COMPLETED = "onboarding_completed"

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
