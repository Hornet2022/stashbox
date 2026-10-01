"""stashbox 核心 ORM 模型。"""

from .base import Base, TimestampMixin
from .user import User
from .article import Article
from .distilled_article import DistilledArticle
from .feedback import Feedback
from .feedback_v2 import FeedbackV2
from .tag import Tag, TagSubscription
from .admin_operation_log import AdminOperationLog
from .favorite import Favorite
from .later_listen import LaterListen
from .system_config import SystemConfig
from .push_notification import PushNotification
from .listening_status import ListeningStatus
from .distillation_evaluation import DistillationEvaluation  # CP3.7.1 §2.1.A
from .user_listening_pattern import UserListeningPattern  # CP3.7.1 §2.1.B
from .article_audio_variant import ArticleAudioVariant  # CP3.7.1 §2.1.C
from .few_shot_example import FewShotExample  # CP3.7.1 §2.1.D
from .consent import ConsentRecord  # CP5.6.0 §3.1
from .tts_voice import (  # CP-TTS-VOICE 音色库 + 用户偏好
    DEFAULT_PLAYBACK_SPEEDS,
    MAX_PLAYBACK_SPEED,
    MIN_PLAYBACK_SPEED,
    TTSVoice,
    UserTTSPreference,
)

__all__ = [
    "Base",
    "TimestampMixin",
    "User",
    "Article",
    "DistilledArticle",
    "Feedback",
    "FeedbackV2",
    "Tag",
    "TagSubscription",
    "AdminOperationLog",
    "Favorite",
    "LaterListen",
    "SystemConfig",
    "PushNotification",
    "ListeningStatus",
    # CP3.7.1 听感产品化数据底座
    "DistillationEvaluation",
    "UserListeningPattern",
    "ArticleAudioVariant",
    "FewShotExample",
    # CP5.6.0 个性化 + 隐私政策 v2
    "ConsentRecord",
    # CP-TTS-VOICE 音色库 + 用户音色/语速偏好
    "TTSVoice",
    "UserTTSPreference",
    "DEFAULT_PLAYBACK_SPEEDS",
    "MIN_PLAYBACK_SPEED",
    "MAX_PLAYBACK_SPEED",
]
