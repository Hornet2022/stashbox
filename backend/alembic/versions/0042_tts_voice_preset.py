"""tts_voices 加 preset_voice 列（声明式预设音色 = 固定说话人，2026-11-25）

为什么
----
问题一「分段换人感」的**根因**不是接缝多，而是**零样本克隆逐请求采样说话人**：
每一块是一次独立请求，各采一个「接近参考音」的人，接缝处两人是否一致是**概率事件**
（生产实证 ≈12% 接缝抽到明显不同的人；块长 250→1500 把接缝从 ~9 减到 1，
大幅降低但**非归零**，首批重生成 18 篇里 2 篇原本同一人被改坏）。

⇒ 真正消除换人要**固定说话人**。当前上游（hub.tsingfly.com，
Qwen3-TTS-12Hz-1.7B-CustomVoice）的**预设音色**就是固定说话人：
不传 ref_audio ⇒ clone=None ⇒ voice 名生效，跨请求身份稳定
（2026-11-25 生产实证：8 块整段两两 28 对 min 0.8629，零对 ≤0.70；
接缝 ±4s 窗口全 ≥0.8519）。且预设 `uncle_fu` 与克隆源婷婷是**同一个人**
（ECAPA 0.8849，落在同人带 0.857~0.931）⇒ 切预设不改变「谁在念」，新旧文章兼容。

为什么是音色库的一列，而不是全局配置开关
--------------------------------------
音色库是「有哪些音色」的**唯一真相源**。某行声明 `preset_voice='uncle_fu'`
= 「这个音色就是上游预设 uncle_fu（固定说话人）」，蒸馏解析到它就走预设路径。

不用全局开关的理由是**溯源诚实无法闭合**：全局开关一开，所有音色都走预设，
但 `distilled_articles.tts_voice_id` 记的仍是音色库那行 —— 若那行其实是个克隆音色，
溯源就在说谎（记了一个没参与合成的音色）。声明式把「走不走预设」绑在**音色行自己**
身上：声明了预设 ⇒ 预设真被使用 ⇒ applied=True ⇒ 记 voice_id 诚实；
没声明 ⇒ 走克隆 ⇒ 按克隆规则溯源。两条路径各自诚实，互不污染。

NULL 而不是空串
--------------
NULL = 未声明预设 = 零样本克隆音色（**既有行为，默认**）。
空串在 service 层归一为 NULL（`strip() or None`，与 description 同口径）。
0042 之前的行 preset_voice 为 NULL ⇒ 行为与现在完全一致（克隆）。不回填。

下游
----
* ``common/tts_voice_service.py``：ResolvedVoice 带 preset_voice；create/update 支持它。
* ``ai-service/agent/tools.py``：preset_voice 非空且 provider=openai ⇒ 走预设
  （synthesize 只带 voice、**不带 ref_audio/ref_text**），applied=True；否则走克隆。
* ``content-service/tts_voice_router.py``：admin 试听同走预设分支（试听=生产渲染）。

运维步骤（迁移**不**覆盖的部分）
-------------------------------
本迁移**只加列、不回填**（理由见上），所以「谁声明了预设」是**数据状态**，不是迁移产物：

* 生产库现状（2026-11-27 核实）：默认音色行
  ``ttsv_f6eeb0e6f91d4ecbb17093b0 / tingting-clone / 婷婷`` 的
  ``preset_voice='uncle_fu'``，由 admin 接口**手工声明**。
* ⇒ **新环境 / 重建库 / 换库**必须补这一步：
  ``PUT /admin/tts/voices/{id}`` 带 ``{"preset_voice": "uncle_fu"}``。
* ⚠️ **漏了这一步不会报错，只会静默回落克隆路径** —— 后果有两处：
  ① 换人风险回到 ≈12% 接缝失败率（预设的根治性消失）；
  ② **内容完整性防线退档**：``openai._truncation_cps_limit`` 按「路径 + 块长」分档，
     严格档 6.0 只在预设路径（clone=off）生效，克隆路径走宽松档 11.0
     —— 而 11.0 曾把实测的 8.14 / 9.44 字/秒截断**全放过去**
     （见 docs/TTS音色一致性契约_v1.md §2.6 与提交 907f678 的正文）。
  ⇒ **声明预设 = 装上防线**，两件事是同一件事的两面。

已知边界
--------
未声明预设的克隆音色**仍有**概率性换人风险（本轮不改克隆机制）。
非 openai provider（如 indextts）即使声明了 preset_voice 也回落克隆
（预设是 openai 托管端点的能力，indextts 的音色由参考音频决定）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tts_voices",
        sa.Column(
            "preset_voice",
            sa.String(length=64),
            nullable=True,
            comment="上游预设音色名（固定说话人）；NULL=零样本克隆音色",
        ),
    )


def downgrade() -> None:
    op.drop_column("tts_voices", "preset_voice")
