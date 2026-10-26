"""content-service Pydantic schemas（CP1.7）。

从 main.py 抽出，路由只做编排，模型定义集中在此。
"""

from typing import Optional

from pydantic import BaseModel, Field


class AddArticleRequest(BaseModel):
    url: str
    source: str = "web"  # wechat | douyin | web | pdf | d9 | clawbot
    # 运营手动录入时会传标题。之前这里**没有**这个字段，而 admin-web 的
    # createArticle 一直在 body 里带 title —— Pydantic 默认忽略未声明字段，
    # 于是标题被静默丢弃，运营填完看到"创建成功"但库里没有标题。
    # 保持 optional：老客户端不传时行为不变（走 fetcher 抓标题）。
    title: Optional[str] = None


class AdminArticleRequest(AddArticleRequest):
    """运营录入专用请求体（2026-10-24 产品拍板后加的 `tags`）。

    ## 为什么单独一个类，而不是给 [AddArticleRequest] 加字段

    产品决策：「admin 录入的内容……**订阅了分类就可以看到，没订阅就看不到**」
    ⇒ 运营录入时必须能**指定分类**。

    但这个字段**不能**加到 [AddArticleRequest] 上 —— 那是**用户侧剪藏**入口的
    请求体。加上去等于把「用户可以自己给自己的内容指定分类」也开放了，
    而那是另一个产品决策（现在用户剪藏的分类由 LLM 猜）。

    ⇒ 单独一个子类：只有 admin 端点用它，用户侧入口的契约一个字没变
    （旧客户端不传 `tags` 也能正常用）。

    ## `tags` 的语义

    · 存**中文标签名**（"科技"），不是 slug（"tech"）——
      推送与订阅都是按 name 精确匹配（`Tag.name.in_(da.tags)`）；
    · 空 = 不指定 → 蒸馏时用 LLM 猜的分类；
    · 指���了 → **优先用运营指定的**（LLM 猜的作废），
      理由见 `articles.manual_tags` 列的注释。
    """

    tags: list[str] = Field(default_factory=list)


class TTSVoiceBrief(BaseModel):
    """这段音频的合成音色（CP-TTS-VOICE 溯源）。

    `available=False` = 该音色已下架/已删除，但**名字照给** —— 历史音频确实是
    它合成的，藏掉名字会让来源凭空消失，用户反而以为自己记错了。
    """

    id: str
    name: str
    available: bool = True


class ArticleResponse(BaseModel):
    id: str
    url: str
    source: str
    title: str | None = None
    owner_id: str
    status: str  # pending | distilling | ready | listened | failed
    # 蒸馏当前阶段（fetch/rewrite/tts/concat），仅 distilling 时非空。
    #
    # 为什么需要：蒸馏是异步长任务，TTS 合成占了总时延的大头
    # （上游 RTF≈0.3，1000 字稿要合成 4 分钟音频）。
    # 客户端只看到 status=distilling（「蒸馏中」）时无从判断是在正常干活
    # 还是卡死 —— 这个字段让 App 能显示「正在合成语音…」。
    #
    # 阶段值直接取自 agent 的 current_step（common/distill_stage.py），
    # 不另造一套枚举；蒸馏结束/失败即清除（终态由 status 表达）。
    stage: str | None = None
    favorite: bool
    skip: bool
    created_at: str
    audio_url: str | None = None
    task_id: str | None = None
    duration_sec: int | None = None
    # CP-TIME：蒸馏完成时间（status=ready/failed 时有；distilled_articles.updated_at）
    distilled_at: str | None = None
    # CP-TIME：文章最近一次状态更新时间（created_at 提交；distilling 时=trigger_distill 时刻；
    # failed 时=最近一次失败；ready 时=蒸馏完成）—— 蒸馏中心失败列表用此字段
    updated_at: str | None = None
    # CP-TAG-FILTER：标签名（蒸馏 LLM 自动生成的中文名，如 ["科技","财经"]）；
    # pending/distilling 时为 None（未生成）。与 GET /api/v1/articles?tag=xxx 配合。
    tags: list[str] | None = None
    # CP-DISTILL-TEXT：LLM 听感改写稿全文（hook/body/outro 以空行分段）。
    # 详情页展示"整理后的正文"。蒸馏未完成时为 None。
    script_text: str | None = None
    # 三类再创作内容的**分区**（2026-10-23）。
    #
    # {"sections": [...], "insights": [...], "related": [...], "follow_ups": [...]}
    #
    # 为什么需要它：这四类在改写 JSON 里本来就是分开的，但落库时被
    # **合并进 script_text**（TTS 要连着念）—— 合并之后客户端就分不清
    # 「关联思考」和「正文要点」，只能把所有段落平铺显示。
    # ⇒ 这三类内容**一直在生成，用户却看不到**。
    #
    # 形状说明：
    # · `sections` = 正文要点（不含 hook/outro，也不含下面三类）
    # · `insights` = 延伸判断 / `related` = 关联思考 / `follow_ups` = 拓展方向
    # · `hook` / `outro` 仍在 script_text 的首末段（老客户端不受影响）
    #
    # **只在详情页填**（列表恒为 null）：分区内容有字数，列表每行挂一份
    # 会让响应膨胀好几倍。
    # 蒸馏未完成 / 0039 之前的历史数据 → None（客户端按「无分区」处理）。
    content_parts: dict | None = None
    # CP-TTS-VOICE 溯源：这段音频当时用哪个音色合成的。
    # None 有两种含义，客户端无法也不必区分：迁移前跑的历史文章（无从得知）、
    # 以及用全局配置合成的（source 不可溯源，刻意不塞假音色）。
    #
    # ⚠️ **只在详情页填**，列表接口恒为 null：列表每行都挂个音色名是噪音，
    # 而且要为此在列表查询里 join 一次音色表。详情页和 /status 才需要它 ——
    # 后者是「换音色重生成」时 App 轮询的那个端点，不带缓存，重生成后能立刻看到音色变了。
    tts_voice: TTSVoiceBrief | None = None
    # 0041：用户剪藏内容共享（2026-10-26 产品拍板）。
    #
    # `shared` = 这篇是否已共享给订阅者（true 表示订阅了 `shared_tags` 里
    # 任一标签的其他用户能通过订阅流看到它）。**属主永远能看自己的内容**，
    # 这个字段只描述「对别人是否可见」。
    #
    # `shared_tags` = 用户共享时手动选的公开标签（中文标签名数组）。
    # 与 `tags`（LLM 分类）**并存**：共享是「额外公开」，不覆盖 LLM 的分类。
    #
    # ⚠️ 这两个字段**只对属主**有意义：订阅者看到的是别人的内容，
    # 「我能不能共享它」无从谈起。所以订阅者视角下恒为 `shared=false` /
    # `shared_tags=null`（不泄露属主的共享意愿）。
    shared: bool = False
    shared_tags: list[str] | None = None


class ShareRequest(BaseModel):
    """0041：用户**共享**一篇文章时提交的公开标签（2026-10-26 产品拍板）。

    - `shared_tags`：中文标签名数组，去重后落 `articles.shared_tags`（JSONB）。
      空数组 / 缺省 ⇒ **取消共享**（置 NULL，等价于「不再对订阅者公开」）。
    - 只接受**属主**调用；订阅者调共享接口一律 403。
    - 标签是**顺序无关**的集合语义：服务端去重、不保证顺序。
    """

    shared_tags: list[str] = []


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
    # 蒸馏当前阶段（fetch/rewrite/tts/concat），仅 distilling 时非空。
    # App 轮询这个端点等 ready，靠它显示「正在合成语音…」而不是干等
    # 笼统的「蒸馏中」（TTS 是最慢的一步，1000 字稿要合成 4 分钟音频）。
    stage: str | None = None
    task_id: str | None = None
    task_status: str | None = None  # queued | running | done | failed
    error: str | None = None
    audio_url: str | None = None
    audio_duration_sec: int | None = None
    tags: list[str] | None = None
    quality_score: float | None = None
    created_at: str
    updated_at: str
    # CP-TTS-VOICE 溯源：同 ArticleResponse.tts_voice。放在这里是因为 App 在
    # 重生成期间轮的就是这个端点（且不走缓存），能在蒸馏完成的那一刻看到音色变化。
    tts_voice: TTSVoiceBrief | None = None


class AudioUrlResponse(BaseModel):
    article_id: str
    audio_url: str  # 播放地址：签名 URL 或永久静态直链
    # CP-AUDIO-URL-STATIC：静态直链不会过期，此时为 null。
    # 客户端（Android ArticleDetailViewModel）据此跳过「过期前刷新」——
    # 给静态地址伪造一个 expires_at 会让每次恢复播放都白跑一次本接口。
    # 注意必须 Optional：声明成 str 时返回 null 会直接 500。
    expires_at: Optional[str] = None  # ISO 8601；None = 永不过期
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
