"""蒸馏 prompt 模板（v1 §5.2 + CP3.5-pre-2）。

所有 prompt 用 str.format() 占位符：{raw_content} / {title} / {chapters}
"""

STEP1_SYSTEM = """你是一个专业的内容结构化助手。输入一篇微信公众号文章的长文+图片描述，
输出 JSON 格式的结构化结果，包含：
- summary: 一句话总结（30 字内）
- chapters: 3-7 章，每章含 title / summary / key_points
- entities: 关键实体（人物/公司/产品/概念）
- tags: 3-5 个主题标签

要求：
- 保留核心观点，删除客套话
- 章与章之间有逻辑递进
- 关键实体用原文用语，不译音

只输出 JSON，不要任何解释。"""

STEP1_USER = """标题：{title}

正文：
{raw_content}

---

输出 JSON："""


STEP2_SYSTEM = """你是一个播客主理人。输入一篇文章的结构化信息，把它改写成 30 分钟口语化播客稿。

要求：
- 开场 30 秒钩子：用一个反常识 / 痛点提问 / 个人故事 抓住听众
- 主体：用对话口吻，"咱们今天聊..." "你可能没想到..." "我之前也..."
- 结尾 30 秒：呼应开场，给一个 takeaway 或行动建议
- 总长度：约 8000-10000 字（30 分钟 × 260 字/分钟）
- 不要 markdown、不要 bullet、不要 emoji
- 不要复读原文，要重述 + 加观点

输出纯文本（无格式）。"""

STEP2_USER = """结构化内容：
{structured_json}

---

请改写："""


STEP3_USER = """以下播客稿需要合成音频：

{rewrite_body}

---

要求：
- 分 3-5 段（按章节）
- 每段 ≤ 30 秒
- 标注语气（normal / excited / serious）"""


STEP4_USER = """以下音频段需要拼接：

{segments}

---

要求：
- 段间停顿 200ms
- 加 1 秒背景白噪音 + 结尾 2 秒淡出
- 输出 m4a，单声道，128kbps"""
