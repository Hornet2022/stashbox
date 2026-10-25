"""OpenAI 协议 TTS 客户端（CP8.3 真实化）。

这是一个**通用 OpenAI 兼容 TTS 客户端**，可用于：
- 火山方舟 ARK：base_url=https://ark.cn-beijing.volces.com/api/v3，ARK 平台开通 Doubao TTS 模型后取 API Key
- OpenAI 官方：base_url=https://api.openai.com/v1
- Azure OpenAI：base_url=https://{resource}.openai.azure.com/openai/deployments/{deployment}
- 其他 OpenAI-compatible TTS 端点

请求体（OpenAI 兼容）::

    POST {base_url}/audio/speech
    Authorization: Bearer <api_key>
    {
        "model": "doubao-tts",     # provider-specific 模型标识
        "voice": "BV001_streaming", # provider-specific 音色 ID
        "input": "...",
        "response_format": "mp3",
        "speed": 1.0
    }

响应：raw audio bytes（Content-Type: audio/mpeg）。

⚠️ **「分段音色差异」的根因与修法（2026-10-24 实测闭合）**

    症状：内测反馈「历史音频分段音色差异」—— 一篇稿子里像好几个人在念。

    根因**不在我们**：托管端点（tertu.cn / Qwen3-TTS-12Hz-1.7B-Base）
    **不认 `voice` 这个说话人名**。同一段文本、同一个 `voice=uncle_fu`
    连发 3 次，基频中位数 165.0 / 188.5 / 105.3 Hz —— 三个不同的人，
    时长 7.60 / 6.88 / 6.88s。

    我们按 700 字分块 → 每块一次请求 → **每块一个人**。
    生产 317 篇的分块分布：1 块 1 篇、2 块 154 篇、3 块 111 篇、最多 7 块
    ⇒ 几乎每篇都有音色接缝。

    换模型没用（实测 `Qwen3-TTS-12Hz-1.7B-CustomVoice` 同样随机：
    145.2 / 151.5 / 190.9 Hz）。

    **修法只有一条：让零样本克隆真的生效。** 同一份 `ref_audio` 连发 3 次
    → 基频 217.9 / 219.3 / 224.7 Hz（±1.6%），其中两次字节完全相同。

    所以本 provider 现在**透传** `ref_audio` / `ref_text`（见 `synthesize`）：

    ① `ref_audio` 必须是 **data: URL**（`data:audio/wav;base64,...`）。
       裸 base64 上游不认 —— 它会**超时**（504），而不是报错：
       请求合法，只是上游读不出音频 → 干等 60s 墙钟。
    ② `ref_text` 必须同时给，缺了上游 400。
    ③ 克隆模式下上游**完全忽略 `voice`**（实测连非法 voice 名都返回 200），
       音色由参考音频决定。

    复现与验收见 `backend/scripts/check_tts_voice_consistency.py`
    （实测分块渲染后各块基频中位数差异 < 5%）与
    `backend/tests/tts/test_openai_clone_ref_audio.py`（契约测试）。

⚠️ **长稿必须分段合成**（2026-10-21 生产实测踩过）：

    1503B / ~500 字  → 40s  ✅
    1636B / ~545 字  → 41s  ✅
    3142B / ~1047 字 → 60s  ❌ 超时失败（httpx 固定 60s 墙钟）

合成耗时随字数近似线性增长，而超时是**固定值** → 长稿必然击穿。
表现是：LLM 改写正常、script_text 也写进库了，但音频没出来，
文章卡在「蒸馏失败」，用户看到的稿子是对的、音频却是空的。

所以这里按句读边界切块 → 逐块合成 → 拼接（见 audio_chunks.py）。
IndexTTS provider 早就因为同类问题做了分段，这套实现现在共用。

⚠️ **分段要串行，不要并发**（同一天实测，3 段 × ~280 字）：

    并发=1 → 总耗时 54.2s，每段 17.4 / 37.6 / 54.1s
    并发=2 → 总耗时 51.1s，每段 16.4 / 36.2 / 51.1s
    并发=4 → 总耗时 53.4s，每段 53.4 / 20.9 / 34.2s

三种并发总耗时几乎一样 → **上游是串行处理的**，并发对它零吞吐贡献。
但并发 4 时最后一段要等 ~53s 才轮到处理，而 httpx 的读超时是**从请求
发出开始**算的 → 4 段并发时最后一段必然撞 60s。生产实跑确实这么炸过：
第 4 段在 60s 超时，整篇蒸馏失败（前 3 段都成功了）。

串行则每段 17s 完成，对 60s 超时有 3.5 倍余量，总耗时不变。

⚠️ **上游会抖动，单块超时 + 单块重试**（同一天实测）：

    段1 16s ✅   段2 60s 超时 ❌   段3 16s ✅   ← 同一篇稿子的三段

所以超时从 60s 放宽到 120s（正常段 13~20s，6 倍余量），
并且单块失败时**只重试这一块**（OPENAI_TTS_CHUNK_RETRIES，默认 2，
退避 3s/6s）—— 不必让 Arq 把整篇（LLM 改写 + 全部分块）重来一遍。

火山引擎方舟 TTS 模型示例（2026-09-21 官方文档）：
- 模型：doubao-seed-tts（或按控制台开通的实际模型名）
- 音色 ID：BV001_streaming 等（控制台 → 音色库 → 复制 speaker）
- 端点：https://ark.cn-beijing.volces.com/api/v3/audio/speech
- 鉴权：Bearer <ARK_API_KEY>（方舟控制台 → API Key 管理）

依赖: httpx（已在 requirements.txt）
凭证: OPENAI_TTS_API_KEY（与 LLM 端的 OPENAI_LLM_API_KEY 拆开，CP9.x 决策）

## ⚠️ 上游到底接受哪些字段（2026-10-23 对生产端点实测，别凭直觉猜）

生产端点是 `https://tertu.cn/v1`（元涌平台托管的 Qwen3-TTS）。它的请求体
是 **Pydantic 校验**的 —— 传错类型会返回 400 并在 `upstream_body` 里点名
字段，于是可以**用「类型错误」反推哪些字段真的被识别**（未知字段会被静默
丢弃、连 400 都不给，所以「传了没报错」不能作为「支持」的证据）。

实测结论（模型 `Qwen3-TTS-12Hz-1.7B-CustomVoice`）：

| 字段 | 上游认吗 | 实测依据 |
|---|---|---|
| `voice` | ✅ 枚举 | 传错 → 400「Invalid voice 'x'. Supported: aiden, dylan, eric, ono_anna, ryan, serena, sohee, uncle_fu, vivian」 |
| `instructions` | ✅ string | 传数字 → 400「Input should be a valid string」（**风格控制 prompt**） |
| `language` | ✅ string | 传数字 → 400 |
| `speed` | ✅ number | 传字符串 → 400 |
| `seed` | ✅ int | 传字符串 → 400，**但传了也不影响生成**（见下） |
| `response_format` | ✅ | `mp3` → `audio/mpeg`；缺省 → `audio/wav` |
| `temperature` / `top_p` / `top_k` / `repetition_penalty` | ❌ **不在 schema** | 传字符串 `"abc"` 仍返回 200 → 被静默忽略 |
| `instruct` | ❌ | 传数字仍 200 → 未知字段。风格控制叫 **`instructions`** |

### 「固定 temperature / top_p」这条要求在这条链路上做不到 —— 别假装能做到

官方对「稳定播音」的建议是 temperature=0.7 / top_p=0.9，但**托管端点根本
不收这两个参数**（schema 里没有，传了被丢）。实测也证实它们无效：

    同一句话、同一个 seed=42，连发两次 → PCM 长度 168960 / 142080 bytes（差 19%）
    temperature=0.0  连发两次         → PCM 仍不同
    seed 固定 / 不固定               → 差异量级相同（seed 无效）

即**上游每次请求都是一次全新的独立随机采样**，没有跨请求的一致性保证。
所以「分段音色一致」在这条链路上**不能靠请求参数**，只能靠三件事：

1. `voice` 固定（音色身份）—— 本类每段都读同一个 `self.voice`；
2. `instructions` 固定（风格）—— 本类每段都读同一个 `self.instructions`；
3. **后处理对齐**（音域 + 响度）—— 见 `audio_chunks.concat_audio` 与
   `voice_align.py`。上游随机漂的那部分，靠这一步拉回来。

其中第 3 步是**真正兜底**的那一步：音高对齐按各段 p25（说话人音域下缘）
统一到全篇锚点，响度归一逐段做。实测生产日志里对齐生效
（`TTS 音高对齐: semitones 0.67 / -1.02 / 0`）。

自建端点（`indextts` provider → 本机 mlx-audio）**能**收 temperature/top_p，
那边的实现在 `indextts.py`；但生产没有走那条路。
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .audio_chunks import concat_audio, split_into_chunks
from .base import TTSClient
from .ref_audio import RefAudioError, load_ref_audio_data_url

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CloneSpec:
    """一次合成要用的克隆音色（参考音频 + 参考文本）。

    ⚠️ `data_url` 是**整篇共用同一份**的：分段合成是多次独立请求，
    每一次都必须带**逐字节相同**的参考音频 —— 只要有一块带了别的参考音，
    那一块就是另一个人说话（这正是本次要修的「分段音色差异」）。
    所以它在 `synthesize()` 里解析一次，然后逐块传下去。
    """

    source: str
    data_url: str
    text: str


# OpenAI 默认 base_url（覆写后可指向任意 OpenAI 兼容 TTS 端点）
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "tts-1"
_DEFAULT_VOICE = "alloy"

# 「输出疑似被静默截断」的语速阈值（字/秒）。
#
# 端点只念了开头时，字数没少、时长偏短 → **语速虚高**。
# 判据与 IndexTTS 的 _assert_not_truncated 同源（那里正常 4.41~4.63、
# 真截断 8.35、阈值 7.0）。这条链路的正常区间更宽，阈值另标（见下）。
#
# ⚠️ **阈值是量出来的，不是拍的**（2026-10-25，用
#    scripts/calibrate_tts_chunk_rate.py 在生产**真实分块**上实测）：
#
#     样本（当日，同一上游 tertu.cn / Qwen3-TTS-12Hz-1.7B-CustomVoice）
#         250 字克隆块  n=20   5.15 ~ 9.65   中位 ~7.0
#         800 字单次    n=1    7.50
#         909~983 字块  n=6    5.56 ~ 6.49
#         短块（40~60 字，生产日志）  6.56 ~ 9.40
#
# ⇒ 正常上界取 **9.65**，阈值取 **11.0**（留 14% 余量）。
#   真截断（音频只剩一半左右）会是 13~19 字/秒，仍被抓住。
#
# ⚠️ **为什么不是「按块长分档」**：实测 cps 与**块长无关**，只与**文本内容**
#   有关（同一个 250 字块：5.96 ~ 9.65）。分档只会凭空造出结构。
#   ⇒ 一个阈值，按今天的分布标定。
#
# ⚠️ **老阈值 6.8 是给另一个模型标的**（2026-10-24）：
#   那次标定用的是 **Qwen3-TTS-12Hz-1.7B-Base**（正常 4.80~6.45 字/秒），
#   生产现在跑的是 **CustomVoice**（正常 5.15~9.65）—— 语速整体快了约 1.5 倍，
#   阈值 6.8 于是落进**正常区间内部** ⇒ 近 24h 生产误判 **107 次**
#   「分块被截断，二分重来」（块长 239~250 字，即正常块），
#   每次自愈都把一块二分 → **凭空多出 1~2 个音色接缝**，
#   而接缝正是「一篇音频听出好几种音色」的来源。
#   ⇒ **阈值必须跟着「当前在用的模型 + 当前上游行为」重新标**，
#     换模型/换上游后要重跑 calibrate_tts_chunk_rate.py。
#
# 误报的代价不只是多报一次：短块（≤ min_chunk_chars）→ 异常直接上抛，
# **admin 的「TTS 测试」按钮对 openai provider 永远是红的**；长块 → 触发
# 重试（10s/30s 退避）与二分 = 把这一段**重新采样一遍**，每次重采样都是
# 一次新的随机采样，分段音色离散度被推高，还平白多出新的接缝。
#
# 「宁可误报也不要漏报」这个取舍**改成「误报不致命」**：
# 阈值放到 11.0 之后误报变少，但短块仍可能撞上上限
# ⇒ 短到**没法再切**的块（< _MIN_SPLIT_CHARS）改为**告警并保留音频**，
#   不再让异常上抛把整篇拖挂（2026-10-24 实测：47 字的块被判 9.40 字/秒
#   → 整篇挂掉，靠 Arq 重试才救回来）。
#
# ⚠️ **测带宽必须用真实分块**（这是 2026-10-24 踩过的坑）：
#   取数时若把稿子按换行切开，量到的就是**碎片** —— 碎片尾部没有句号与停顿，
#   读得偏快 → cps 虚高（实测同一档位：碎片 6.93，真实分块 6.45），
#   拿它定阈值会把阈值**定得过紧**。
#   正确取法见 scripts/calibrate_tts_chunk_rate.py（清洗换行 + 生产切块逻辑）。
_TRUNCATION_CHARS_PER_SEC = 11.0

# 二分自愈的最大递归深度（2^6 = 64 段，兜底防无限递归）。
_MAX_SPLIT_DEPTH = 6

# 二分自愈能切到的**绝对下限**（字）：文本本身比它还小，就不再切了。
#
# ⚠️ 与 `min_chunk_chars`（60）是两回事：
#   min_chunk_chars = 「自愈的最小粒度」，是**给长块**用的软约束；
#   _MIN_SPLIT_CHARS = 「再切就没意义了」的硬地板。
#
# ⚠️ **地板是「被切的文本」的下限，不是「切出来的块」的下限**：
#   硬切会留下 1 字的余块（47 字切成 23/23/1），
#   若拿「最小切块」当条件，整次切分会被否掉 → 自愈根本不会发生。
#
# 取 12 的依据：上游对 8 个字能正常返回 1.0s 音频（不报错），
# 说明 ~10 字量级的请求上游接得住；而再小就只是产生更多块、
# 在同一个噪声判据上继续打转。
_MIN_SPLIT_CHARS = 12

# ── 克隆路径的块长上限（字）────────────────────────────────────────────
#
# ⚠️ **2026-10-25：250 → 800**。老值错在**把「音频时长」当成了「墙钟」**：
#
#    当时的依据是「250 字 → 音频 43.7s，占 60s 墙钟的 2/3」。
#    但**音频时长 ≠ 上游处理耗时** —— 上游是**快于实时**生成的：
#
#        字数      墙钟(实测)     音频时长      语速
#         250 字      12.1s        36.8s       6.79
#         800 字      31.8s       106.6s       7.50
#        1500 字      58.8s          —          —
#
#    ⇒ 墙钟约 **40ms/字**，与音频时长无关。
#    老算法因此**低估了上游的能力**（拿 43.7s 当墙钟 → 只敢用 250 字）。
#
#    取 800 字：墙钟 31.8s，对 60s 墙钟留 **1.9 倍余量**
#    （上游墙钟是负载相关的：同一个 1158 字的稿子 43.89s 成功过、60.2s 也 504 过）。
#    1000 字约 40s（1.5 倍余量）—— 余量被上游负载吃掉的风险明显更大，不取。
#
#    **收益是结构性的**：段数直接砍到 1/3，接缝也跟着砍到 1/3。
#    生产实测（2026-10-25，journal）：2544 字的稿子 → **12 段**（11 个接缝），
#    2170 字 → **10 段**。分段 = 多次独立采样 = 多个「音色接缝」，
#    而接缝正是「一篇音频听出好几种音色」的来源。
#
#    **安全网还在**：块放大后若撞上上游 60s 墙钟（504）或被静默截断，
#    二分自愈会把那块拆小重渲（见 _synthesize_chunk）—— 所以放大是安全选项。
#    实测上游对 1500 字返回 200（58.8s）也说明它的实际余量比 60s 大，
#    但那不是我们该依赖的。
#
#    不克隆的路径保持原样（700 字）：音色本来就是随机的，块多块少都一样糟，
#    而块少能少几次请求、少几次接缝。
_CLONE_CHUNK_CHARS_DEFAULT = 800

# ⚠️ **2026-10-25：按长度分档被撤掉了** —— 重新标定后，
#    **实测 cps 与块长无关**（250 字块 5.15~9.65、短块 6.56~9.40、
#    800 字 7.50、909~983 字 5.56~6.49），差别全来自**文本内容**。
#    为「不存在的结构」保留两套阈值，只会让阈值看起来更讲究、其实更糊。
#
#    ⇒ 现在**一个阈值走天下**（_TRUNCATION_CHARS_PER_SEC = 11.0）。
#    而「短块误伤会让整篇挂掉」这个真实风险改用**另一条机制**兜：
#    短到**没法再切**的块（< _MIN_SPLIT_CHARS）→ 告警并保留音频，不上抛。
#    （见 _assert_not_truncated。）
#
#    ⇒ 原 `_MIN_CHARS_FOR_RATE_CHECK` / `_SHORT_TEXT_MAX_CPS` 已删除，
#      旧结论（10-24，Base 模型）保留在上面的注释里，别再照抄回代码。


class _TruncatedOutput(RuntimeError):
    """端点返回了 HTTP 200 但内容被静默截断（只念了开头）。"""


class _UpstreamTimeout(RuntimeError):
    """上游自己的 60s 墙钟把请求砍了（HTTP 504 `backend_timeout`）。

    ## 为什么它也要走二分自愈

    它**不是**我们的超时（我们的 timeout 是 300s，远没那么容易到），
    但后果一样：这次合成拿不到内容。

    实测（2026-10-22）：同一个 1158 字的稿子，
    43.89s 成功、60.2s 就 504 —— 上游的生成速度随负载波动 1.4 倍，
    足以翻过它那条 60s 的线。

    之前这个 504 走不到二分自愈（自愈只认客户端超时和截断），
    于是**白重试 3 次**（每次都占满上游 60 秒）才失败 ——
    而把段二分、更短，就不会撞墙钟。
    """


# ── 熔断 ────────────────────────────────────────────────────────────────────
#
# 上游 TTS 有 **60s 墙钟**且是**共享**资源：一次大请求会占住它 60 秒，
# 期间其他请求全部排队 → 连锁 504。
#
# 实测（2026-10-22）：连续打大段长请求把上游压满后，
# **真实用户的剪藏全部失败**（连续 30 分钟 504 率 100%）。
#
# 所以必须有熔断：连续失败就**快速失败**，别再往上游压 ——
# 熔断期内任务立刻返回失败，Arq 层会退避后重试，
# 上游才有喘息恢复的时间。硬扛只会让它一直不恢复。
#
# 半开状态：熔断期满后放一个请求探路，成功则完全恢复，失败则继续熔断。
_CIRCUIT_FAIL_THRESHOLD = 3
_CIRCUIT_OPEN_SECONDS = 60.0

_circuit_state = {"consecutive_failures": 0, "opened_at": 0.0}


class _CircuitOpen(RuntimeError):
    """熔断打开中：上游疑似不可用，**不要**再打它。

    带 `retry_after`（秒）—— 蒸馏层据此把退避拉长到熔断窗口那么久，
    否则 15s 的固定退避会在熔断窗口内把 Arq 的重试次数全部烧光
    （5 次 × 15s = 75s，而熔断窗口 60s —— 大半次重试都打在「拒绝服务」上）。
    """

    retry_after = _CIRCUIT_OPEN_SECONDS


def _circuit_allow() -> bool:
    """当前是否允许发请求。

    关闭态 → 放行。
    打开态（距上次打开 < `_CIRCUIT_OPEN_SECONDS`）→ 拒绝。
    半开态（超过窗口）→ 放行（探路）。成功则完全恢复，失败则重新打开。
    """
    now = time.time()
    opened_at = _circuit_state["opened_at"]
    if not opened_at:
        return True
    if now - opened_at < _CIRCUIT_OPEN_SECONDS:
        return False
    return True


def _circuit_record_success() -> None:
    _circuit_state["consecutive_failures"] = 0
    _circuit_state["opened_at"] = 0.0


def _circuit_record_failure() -> None:
    _circuit_state["consecutive_failures"] += 1
    if _circuit_state["consecutive_failures"] >= _CIRCUIT_FAIL_THRESHOLD:
        _circuit_state["opened_at"] = time.time()
        log.warning(
            "tts_circuit_opened consecutive_failures=%s open_seconds=%s",
            _circuit_state["consecutive_failures"],
            _CIRCUIT_OPEN_SECONDS,
        )


class OpenAITTSClient(TTSClient):
    """通用 OpenAI 协议 TTS 客户端（HTTP POST → audio bytes）。

    通过 OPENAI_BASE_URL 切换底层 provider：
      - 火山方舟：https://ark.cn-beijing.volces.com/api/v3，model=doubao-seed-tts
      - OpenAI：https://api.openai.com/v1，model=tts-1/tts-1-hd
      - Azure：https://{r}.openai.azure.com/openai/deployments/{dep}，model 用 deployment 名
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        chunk_chars: int | None = None,
        chunk_concurrency: int | None = None,
        chunk_retries: int | None = None,
        min_chunk_chars: int | None = None,
        instructions: str | None = None,
        language: str | None = None,
        clone_chunk_chars: int | None = None,
    ):
        # 优先入参；入参缺省时从环境变量读（pydantic Settings 不会接管 .env 加载到这里）
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_TTS_API_KEY", "")
        self.model = model if model is not None else os.getenv("OPENAI_TTS_MODEL", _DEFAULT_MODEL)
        self.voice = voice if voice is not None else os.getenv("OPENAI_TTS_VOICE", _DEFAULT_VOICE)
        # ── 风格控制 prompt（instructions）────────────────────────────────
        #
        # CustomVoice 模型（Qwen3-TTS-12Hz-1.7B-CustomVoice）的核心能力：
        # 音色由 `voice`（预设 speaker 名）决定，**风格/情绪/语速**由
        # `instructions` 决定。上游对这个字段做 string 校验（传数字会 400），
        # 所以它是**真被识别**的字段 —— 不是被静默忽略的摆设。
        #
        # 对「分段音色一致性」的意义：分段合成 = 每段一次独立请求，
        # 每次都会重新采样。`instructions` 保证的是**风格取向一致**
        # （实测：同一段文字加「悲伤低沉的语调」→ 时长变长、基频变低；
        # 加「开心轻快」→ 时长变短、基频变高）。
        #
        # ⚠️ 它**压不住**单次采样本身的随机波动（实测同条件下两次请求的
        # p25 相差 65%），所以段间一致性的兜底仍然是后处理的音域/响度对齐。
        # 它的作用是把「平均风格」钉住，避免每段各自发挥。
        #
        # 实测是**实例属性** → 同一篇的每一段读到的都是**逐字相同**的同一份
        # 字符串，这才叫「风格一致」。
        self.instructions = (
            instructions if instructions is not None else os.getenv("OPENAI_TTS_INSTRUCTIONS", "")
        ).strip()
        # 语言标签（上游 string 校验）。中文稿一般不用配，留空即不传。
        self.language = (
            language if language is not None else os.getenv("OPENAI_TTS_LANGUAGE", "")
        ).strip()
        self.base_url = (
            base_url if base_url is not None else os.getenv("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = (
            timeout if timeout is not None else float(os.getenv("OPENAI_TTS_TIMEOUT", "300"))
        )
        # 单块失败时的重试次数（只重试**这一块**，不是整篇重来）。
        #
        # 实测（2026-10-21）：上游会**抖动** —— 同一篇稿子的 3 段里，
        # 段1 16s ✅、段2 60s 超时 ❌、段3 16s ✅，Arq 重试后整篇三段全通。
        # 单块重试能把「某段抖动」消化在本地，不必让 Arq 把整篇
        # （LLM 改写 + 全部分块）重来一遍 —— 那是双倍的钱和双倍的时间。
        self.chunk_retries = (
            chunk_retries
            if chunk_retries is not None
            else int(os.getenv("OPENAI_TTS_CHUNK_RETRIES", "2"))
        )
        # 自愈的最小粒度：块被细到这么小还截断，就不是长度问题（真失败）。
        self.min_chunk_chars = (
            min_chunk_chars
            if min_chunk_chars is not None
            else int(os.getenv("OPENAI_TTS_MIN_CHUNK_CHARS", "60"))
        )
        # 段间静音秒数（0 = 不插）。
        #
        # ⚠️ 现在它只是**兜底**：crossfade 走不通时才用（见
        # audio_chunks.concat_mp3）。主路径不再插静音 ——
        # 实测带着静音拼出来的音频，用户照样听出「三种音色」：
        # 静音只让耳朵「重新分句」，两段依然是两个音色。
        self.gap_sec = float(os.getenv("OPENAI_TTS_GAP_SEC", "0.3"))
        # 单次请求的最大字数。
        #
        # 取 700 的依据（2026-10-22 实测，tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base，
        # 每档连发 3 次）：
        #     400 字 → wall 24.6 / 25.0 / 25.2s  语速 4.5 字/秒  3/3 成功
        #     700 字 → wall 42.5 / 43.3 / 44.3s  语速 4.5 字/秒  3/3 成功
        # 墙钟是**线性**的：约 62ms/字 → 上游 60s 的墙钟 ≈ 965 字上限。
        #
        # **为什么放大段长**：分段 = 多次独立渲染 = 多个「音色接缝」。
        # 实测内测反馈的「一篇三种音色」，在音频轨迹上就定位在
        # chunk2→chunk3 的接缝处（LTAS 跳变 0.39~0.45dB，其余窗只有 0.05~0.15dB）。
        # 一篇 973 字的稿：400 字切 → **5 次渲染 / 4 个接缝**（其中一次是
        # 截断自愈二分出来的），700 字切 → **2 次渲染 / 1 个接缝**。
        #
        # **放大段长的安全网**：上游的 60s 墙钟是**负载相关**的
        # （同一个 1158 字的稿子，43.89s 成功过、60.2s 也 504 过），
        # 所以大段可能撞线 —— 撞了不失败，而是**二分自愈**（见 _UpstreamTimeout）：
        # 更短 → 更不容易撞线。代价是一次 60s 的等待，换来的是不丢内容。
        #
        # ⚠️ 别把段长放到「整篇一次」：实测 1158 字会**静默截断**
        # （返回 200，但只产出 124s 音频，语速 9.3 字/秒 —— 应有 276s）。
        # 静默截断比 504 更危险：它看起来成功了。
        # 我们的 httpx timeout 给到 300s（上游 504 是它自己的限制，给大也没用）。
        self.chunk_chars = (
            chunk_chars
            if chunk_chars is not None
            else int(os.getenv("OPENAI_TTS_CHUNK_CHARS", "700"))
        )
        # 克隆路径的块长上限（**单独一套**：见 _CLONE_CHUNK_CHARS_DEFAULT 的实测）。
        #
        # ⚠️ 2026-10-25：**不再**取 min(普通块长, 这个值)。
        #   两条路径的上游每字墙钟不同（克隆 ≈48ms/字、预设音色 ≈61ms/字），
        #   互夹会凭空砍掉余量。要限制克隆块长就用这个环境变量。
        self.clone_chunk_chars = (
            clone_chunk_chars
            if clone_chunk_chars is not None
            else int(os.getenv("OPENAI_TTS_CLONE_CHUNK_CHARS", str(_CLONE_CHUNK_CHARS_DEFAULT)))
        )
        # 分段并行度。**默认 1（串行）** —— 不是为了性能，是为了让每一段的
        # 计时器从「真正开始被上游处理」起算。
        #
        # 实测（2026-10-21，tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base，3 段 × ~280 字）：
        #     并发=1 → 总耗时 54.2s，每段 17.4 / 37.6 / 54.1s
        #     并发=2 → 总耗时 51.1s，每段 16.4 / 36.2 / 51.1s
        #     并发=4 → 总耗时 53.4s，每段 53.4 / 20.9 / 34.2s
        #
        # 三种并发**总耗时几乎一样** → 上游是串行处理的，并发对它零吞吐贡献。
        # 但并发 4 时最后一段要等 ~53s 才轮到（前面 3 段占着），
        # 而 httpx 的超时是**从请求发出开始**算的读超时 → 4 段以上必然撞 60s。
        #
        # 生产上也确实这么炸过：4 段并发实跑，第 4 段在 60s 超时，
        # 整篇蒸馏失败（19:30:14 发出 → 19:31:14 超时，前 3 段都成功了）。
        #
        # 串行则每段 17s 就完成，对 60s 超时有 3.5 倍余量；
        # 总耗时不变（上游本来就串行），服务器（1.6GB / 2 vCPU）还更省资源。
        self.chunk_concurrency = (
            chunk_concurrency
            if chunk_concurrency is not None
            else int(os.getenv("OPENAI_TTS_CONCURRENCY", "1"))
        )
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            trust_env=False,  # 忽略沙箱/系统代理
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        # 克隆参考音频的 data URL 缓存（同一份参考音只编码一次）。
        #
        # ⚠️ 这里**不能**复用 `self._client` 去下载参考音频：
        # 那个 client 带着 `Authorization: Bearer <TTS key>`，
        # 拿它去请求存储地址等于把 TTS 密钥发给了存储服务。
        # `load_ref_audio_data_url` 自己开一个不带鉴权的 client。
        self._clone_spec: _CloneSpec | None = None

    @property
    def provider_name(self) -> str:
        # 在工厂里叫 "openai"；底层真实 provider 看 base_url
        return "openai"

    def _endpoint(self) -> str:
        return f"{self.base_url}/audio/speech"

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        output_format: str = "mp3",
        ref_audio: str | None = None,
        ref_text: str | None = None,
    ) -> bytes:
        """合成文本为音频字节（OpenAI 协议 /audio/speech）。

        长文本会**自动分段**（按句读边界切块 → 串行合成 → 拼接），
        原因见文件头的实测记录：整篇合成在 ~1000 字时会撞上固定超时。

        Args:
            text: 待合成文本（蒸馏后的"听感版本"）
            voice: 音色 ID（provider-specific；None 走构造时设置的 self.voice）
            output_format: 输出格式（默认 mp3，OpenAI 还支持 opus/aac/flac）
            ref_audio: **克隆参考音频**（本地路径 / http(s) URL / data URL）。
                给了就走零样本克隆，音色由这份音频决定。
            ref_text: 参考音频的**逐字转录**。克隆时必填。

        Returns:
            拼接后的音频 bytes

        Raises:
            ValueError: text 为空 / api_key 缺失 / 克隆缺 ref_text
            RuntimeError: HTTP 异常 / 端点返回错误

        ## 克隆（ref_audio / ref_text）

        这是修复「分段音色差异」的核心（实测记录见模块头）：

        - 托管端点**不认 voice 名**（同一 voice 连发 3 次 = 3 个人），
          唯一能把音色钉死的是零样本克隆。
        - `ref_audio` 必须编成 **data: URL**；裸 base64 上游不认，
          表现是 **504 超时**（不是 400）—— 一次白等 60s。
        - `ref_text` 缺失上游 400，所以**本地就拦**（省一次 60s 请求）。
        - 克隆模式下上游**忽略 voice**（实测非法 voice 名也 200），
          所以这里照常带上 `voice`（不新增风险：非克隆路径本来就在带），
          但音色以参考音频为准。
        """
        if not text or not text.strip():
            raise ValueError("text 不能为空")
        if not self.api_key:
            raise RuntimeError(
                "OpenAITTSClient 需要 api_key。"
                "火山方舟：填 ARK API Key；OpenAI：填 sk-...；Azure：填 api key。"
            )

        clone = await self._resolve_clone(ref_audio, ref_text)

        # ⚠️ 克隆时块长要**另算**（见 _CLONE_CHUNK_CHARS_DEFAULT）：
        #    克隆路径的上游成本与预设音色路径不同 —— 实测每字墙钟：
        #        克隆（带 ref_audio）  ≈ 48ms/字  （250 字 12.1s / 800 字 31.8s）
        #        预设音色（只有 voice） ≈ 61ms/字  （250 字 15.3s）
        #
        #    ⇒ 两条路径各有各的 60s 墙钟预算，**不该互相夹**：
        #    拿更贵那条路径的预算去限制更便宜的那条，等于凭空砍掉一半余量。
        #    （2026-10-25 之前这里是 min(普通块长, 克隆块长)：普通块长 700
        #      会把克隆块长 800 夹回 700 —— 生产上 700 配 800 才是目标值。）
        #
        #    要限制克隆块长，用 `OPENAI_TTS_CLONE_CHUNK_CHARS`；
        #    `OPENAI_TTS_CHUNK_CHARS` 只管**非克隆**路径。
        chunk_chars = self.chunk_chars
        if clone is not None and self.clone_chunk_chars > 0:
            chunk_chars = self.clone_chunk_chars

        if chunk_chars <= 0 or len(text) <= chunk_chars:
            return await self._synthesize_chunk(text, voice, output_format, clone=clone)

        # ── 长稿：切块 → 逐块合成 → 拼接 ──────────────────────────────
        #
        # ⚠️ 为什么不直接调高 timeout：合成耗时随字数近似线性增长，
        # 而「多久算超时」得跟着稿长变 —— 与其猜一个上限（猜小了长稿失败，
        # 猜大了真卡死的请求要等很久才被发现），不如把单次请求压到
        # 已知安全的长度，这样**每次请求都用同一个已知安全的超时**，
        # 行为可预测。
        #
        # ⚠️ concurrency 默认 1（串行），原因见 __init__ 里的实测记录：
        # 上游串行处理，并发零收益，却会让末段在队列里等到逼近超时。
        chunks = split_into_chunks(text, chunk_chars)
        log.info(
            "OpenAITTS 分段合成: total_chars=%d chunks=%d chunk_chars<=%d concurrency=%d clone=%s",
            len(text),
            len(chunks),
            chunk_chars,
            self.chunk_concurrency,
            "on" if clone else "off",
        )

        sem = asyncio.Semaphore(max(1, self.chunk_concurrency))

        async def _one(chunk: str) -> bytes:
            async with sem:
                last_exc: Exception | None = None
                for attempt in range(1, self.chunk_retries + 2):
                    try:
                        return await self._synthesize_chunk(
                            chunk, voice, output_format, clone=clone
                        )
                    except _CircuitOpen:
                        # 熔断打开 → 立刻放弃这一篇，别在这里退避重试：
                        # 退避只会让 Arq 的重试次数烧光，而上游需要的是
                        # **完全没人打它** 的时间。
                        raise
                    except Exception as exc:
                        last_exc = exc
                        if attempt > self.chunk_retries:
                            break
                        # 上游抖动是**间歇性**的：实测同一篇稿子的 3 段里，
                        # 有一段 60s 超时而另外两段 16s 就好了。
                        # 重试前先等一下，别在同一个坏窗口里连打。
                        # ⚠️ 退避要**长**：上游 TTS 有 60s 墙钟且是共享资源，
                        # 撞墙之后上游还有排队积压，短退避只会继续往上游压。
                        # 实测（2026-10-22）：上游被压满时连 80 字短文本都 504。
                        wait = 10.0 * (3 ** (attempt - 1))
                        log.warning(
                            "OpenAITTS 分块失败，重试: attempt=%s/%s wait=%ss err=%s",
                            attempt,
                            self.chunk_retries,
                            wait,
                            exc,
                        )
                        await asyncio.sleep(wait)
                assert last_exc is not None
                raise last_exc

        # gather 保序（返回顺序 = 输入顺序），拼接顺序才不会乱
        pieces = list(await asyncio.gather(*[_one(c) for c in chunks]))
        merged = concat_audio(pieces, gap_sec=self.gap_sec)
        log.info("OpenAITTS 分段合成完成: chunks=%d bytes=%d", len(pieces), len(merged))
        return merged

    async def _resolve_clone(
        self,
        ref_audio: str | None,
        ref_text: str | None,
    ) -> _CloneSpec | None:
        """把「参考音频 + 参考文本」解析成一份**整篇共用**的克隆参数。

        返回 None = 不走克隆（沿用 voice 名的老路径）。

        ⚠️ 为什么在 `synthesize()` 里解析一次、而不是每块各自解析：
        每一块必须带**逐字节相同**的参考音频。分块各读一次文件在
        实践上也没差别，但一旦以后有人给某一块换了来源，
        那一块就是另一个人说话 —— 所以这里把解析点收敛到一个地方。
        """
        src = (ref_audio or "").strip()
        if not src:
            return None

        transcript = (ref_text or "").strip()
        if not transcript:
            # 上游缺 ref_text 会 400；这里本地拦下来，
            # 省一次「白跑 60s 才发现参数不对」。
            raise ValueError(
                "克隆模式必须同时提供 ref_text（参考音频的逐字转录）："
                "上游缺 ref_text 直接 400，而缺 ref_audio 会 504 超时 —— "
                "两者都会白等一次上游。"
            )

        cached = self._clone_spec
        if cached is not None and cached.source == src and cached.text == transcript:
            return cached

        try:
            data_url = await load_ref_audio_data_url(src)
        except RefAudioError as exc:
            raise RuntimeError(f"克隆参考音频不可用: {exc}") from exc

        spec = _CloneSpec(source=src, data_url=data_url, text=transcript)
        self._clone_spec = spec
        log.info(
            "OpenAITTS 克隆音色已就绪: source=%s data_url_bytes=%d ref_text_chars=%d",
            src[:80],
            len(data_url),
            len(transcript),
        )
        return spec

    async def _synthesize_chunk(
        self,
        text: str,
        voice: str | None = None,
        output_format: str = "mp3",
        depth: int = 0,
        *,
        clone: _CloneSpec | None = None,
    ) -> bytes:
        """合成一块文本，**自带截断自愈**：被端点静默截断就自动二分重来。

        ## 为什么需要自愈

        这个端点会**静默截断**长输入：HTTP 200、音频合法，但只念了开头，
        后面的内容凭空消失（与 IndexTTS 的同类坑，见 indextts.py 顶部的实测）。

        实测（2026-10-21，tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base）：

            134 字 → 24.3s → 5.52 字/秒  ✅ 正常（中文播报 4~6 字/秒）
            290 字 → 40.6s → 7.14 字/秒  ⚠️ 截断（按 5.5 字/秒本该 52.7s，丢了 ~67 字）

        判据用**语速**（字/秒）：正常播报 4~6 字/秒，截断会让语速虚高。
        阈值 6.8 离自然上界（6.45）有余量、又稳稳低于真截断的 7.14
        （与 IndexTTS 的 _TRUNCATION_CHARS_PER_SEC 同一判据，取值略严）。

        **只抛错是不够的** —— 同样大小的块重试还是会被截断。
        所以这里把被截断的块**按句读边界二分**后分别合成（内容更短 → 不会被截断），
        再拼回去。递归到 min_chunk_chars 仍截断才算真失败。

        ## 超时也走同一条自愈路径

        上游有 **60s 墙钟**（504 backend_timeout「上游语音合成超时 (60s)」）。
        实测（2026-10-22，tertu.cn 的 Qwen3-TTS-12Hz-1.7B-Base）：

            500 字  → wall=35.7s  音频=130.0s  语速=3.85  ✅
            1000 字 → 504 上游语音合成超时 (60s)          ❌ 必然撞线
            1500/2000/3000 字 → 全部 504                  ❌

        而且这墙钟是**负载相关**的：上游被并发压满时，连 80 字短文本都会被
        误杀（实测：我自己的压测把上游打满后，80 字请求连续 504）。

        所以「超时」和「截断」用**同一个自愈**：把块二分 → 更短 → 更不容易
        撞 60s 墙钟 → 合成 → 拼回。

        这让「段长放大（减少分段 → 减少音色接缝）」成为安全选项：
        上游忙时自动细分兜底，而不是整篇失败。

        ⚠️ `clone` 必须是**逐块相同**的那一份参考音频：
        二分自愈会把一块拆成两块重合成，这两块也必须带同一份参考音，
        否则自愈出来的接缝就是「另一个人」。
        """
        try:
            audio = await self._synthesize_once(text, voice, output_format, clone=clone)
            self._assert_not_truncated(text, audio)
            return audio
        except (TimeoutError, httpx.TimeoutException, _UpstreamTimeout) as exc:
            if len(text) <= self.min_chunk_chars or depth >= _MAX_SPLIT_DEPTH:
                raise RuntimeError(f"OpenAITTS 超时: {exc}") from exc
            parts = split_into_chunks(text, max(1, len(text) // 2))
            if len(parts) < 2:
                raise RuntimeError(f"OpenAITTS 超时: {exc}") from exc
            log.info(
                "OpenAITTS 分块超时，二分重来: depth=%s chars=%d → %d 块",
                depth + 1,
                len(text),
                len(parts),
            )
            pieces = [
                await self._synthesize_chunk(part, voice, output_format, depth + 1, clone=clone)
                for part in parts
            ]
            return concat_audio(pieces)
        except _TruncatedOutput:
            # ⚠️ **这里不能拿 min_chunk_chars 当地板**（2026-10-24 实测踩到）：
            #   块**本来就可能比它小** —— 250 字分块的最后一块常常只剩 40~60 字。
            #   老写法 `len(text) <= self.min_chunk_chars → raise` 于是变成：
            #   短块一旦误判截断 → **直接放弃自愈** → 整篇蒸馏失败。
            #   （实测：47 字的块被判 9.40 字/秒 → 整篇挂掉，靠 Arq 重试才救回来。）
            #
            #   截断路径的真正地板是「**还切不切得动**」：
            #   · 切出来的块必须比原来小（否则这次切分没有进展）
            #   · 文本本身不能小到再切也没意义
            #   ⚠️ 注意：**不能用「所有切块都 ≥ 某下限」当条件** ——
            #   硬切会留下 1 字的余块（比如 47 字切成 23/23/1），
            #   用「最小块」判会把**整次切分**否掉 → 自愈根本不会发生。
            if depth >= _MAX_SPLIT_DEPTH or len(text) < _MIN_SPLIT_CHARS:
                raise
            # 按句读边界二分（复用切块工具，不把词切两半）
            parts = split_into_chunks(text, max(1, len(text) // 2))
            if len(parts) < 2 or max(len(p) for p in parts) >= len(text):
                raise
            log.info(
                "OpenAITTS 分块被截断，二分重来: depth=%s chars=%d → %d 块",
                depth + 1,
                len(text),
                len(parts),
            )
            pieces = [
                await self._synthesize_chunk(part, voice, output_format, depth + 1, clone=clone)
                for part in parts
            ]
            return concat_audio(pieces)

    def _assert_not_truncated(self, text: str, audio: bytes) -> None:
        """语速校验：识别「HTTP 200 但内容被静默截断」。

        判据与 IndexTTS 的 _assert_not_truncated 同源（语速），
        但这里用的是 mp3，所以用 audio_duration 的时长算。

        ## 两条分支，各按各自的长度尺度（2026-10-24 实测标定）

        **长文本（≥ 150 字）→ 语速判据，阈值 6.8**：
        语速 = 字数 / 时长，超过 `_TRUNCATION_CHARS_PER_SEC` 判截断。
        实测这一档的自然带是 5.35~6.45（182~199 字块），
        阈值 6.8 与真截断样本（7.14~8.67）分得开，两侧各留约 5%。
        而且**上游 60s 墙钟只可能砍到这一档的块**（150 字 ≈ 24s）。

        **短文本（< 150 字）→ 语速判据，阈值 8.0**：
        实测自然带是 4.80~6.12（波动最高 19.9%），比长文本宽 ——
        所以阈值必须**按这一档的自然带上界另定**，不能用 6.8
        （短块误伤的代价是整篇失败）。
        8.0 对自然上界留足余量，同时仍高于生产实测的异常值 9.40 ——
        即 9.40 那个异常**会被抓出来**（然后交给自愈重渲）。

        ## 判据抓出来之后呢：自愈必须能救短块

        短块判据**必然**会误伤（带宽就是这么宽），所以真正的修复不在判据，
        在于**误伤之后不能拖死整篇**：块本来就可能比 `min_chunk_chars` 小
        （250 字分块的最后一块常常只剩 40~60 字），
        老写法在那里直接放弃自愈 → 整篇蒸馏失败。
        """
        from stashbox.backend.app.services.tts.audio_duration import audio_duration_exact_sec

        dur = audio_duration_exact_sec(audio)
        if not dur or dur <= 0:
            log.warning("OpenAITTS 音频时长不可解析，跳过截断校验")
            return
        cps = len(text) / dur

        limit = _TRUNCATION_CHARS_PER_SEC
        if cps > limit:
            # ⚠️ **短到没法再切的块不上抛**（2026-10-25 新增）：
            #   误判一次 = 整篇蒸馏失败（实测：47 字的块被判 9.40 字/秒
            #   → 异常直接上抛 → 整篇挂掉，只能靠 Arq 重试救）。
            #   代价对比：
            #     · 保留音频 → 最多这一块语速偏高（听感上「念得快」）
            #     · 上抛异常 → **整篇没了**
            #   ⇒ 明显选前者。二分自愈在 < _MIN_SPLIT_CHARS 时本来也切不动。
            if len(text) < _MIN_SPLIT_CHARS:
                log.warning(
                    "OpenAITTS 短块语速偏高但无法二分自愈，保留音频: chars=%d dur=%.2fs cps=%.2f",
                    len(text),
                    dur,
                    cps,
                )
                return
            raise _TruncatedOutput(
                f"OpenAITTS 输出疑似被截断：{len(text)} 字只产出 {dur:.1f}s 音频"
                f"（{cps:.2f} 字/秒，正常应 < {limit}）"
            )

    async def _synthesize_once(
        self,
        text: str,
        voice: str | None = None,
        output_format: str = "mp3",
        *,
        clone: _CloneSpec | None = None,
    ) -> bytes:
        """单次请求合成（不分段）。分段逻辑在 synthesize() 里。"""
        body: dict[str, Any] = {
            "model": self.model,
            "voice": voice or self.voice,
            "input": text,
            "response_format": output_format,
        }
        # ── 克隆（零样本）：音色由参考音频决定 ───────────────────────
        #
        # ⚠️ `ref_audio` 必须是 **data: URL**。裸 base64 上游不认，
        #    表现是 504 超时而不是 400（请求合法、上游读不出音频 → 干等墙钟）。
        # ⚠️ `ref_text` 必须同时给，否则上游 400。
        # ⚠️ 这一段在**每一次**请求里都要有 —— 分段合成是多次独立请求，
        #    少一块带就等于那一块换个人。
        #
        # 克隆模式下上游**忽略 voice**（实测传非法 voice 名也返回 200），
        # 所以这里照常带 `voice`：不新增风险（非克隆路径本来就在带），
        # 音色以参考音频为准。
        if clone is not None:
            body["ref_audio"] = clone.data_url
            body["ref_text"] = clone.text
        # ── 风格控制：每段逐字相同 ─────────────────────────────────────
        #
        # 这两个字段**每段都带、且每次都一样**（它们是实例属性，
        # 不是按调用传的）—— 分段合成是多次独立请求，这里保证风格不漂。
        #
        # ⚠️ **不要**在这里加 temperature / top_p：实测上游的请求体 schema
        # 里根本没有这两个字段，传了会被**静默丢弃**（连 400 都不报），
        # 于是「以为固定了采样、实际每次都重随机」—— 那比不配更糟，
        # 因为它会让人以为漂移的原因已经排除。详见模块 docstring 的实测表。
        if self.instructions:
            body["instructions"] = self.instructions
        if self.language:
            body["language"] = self.language

        # ── 熔断闸门 ────────────────────────────────────────────────
        # 上游 TTS 是**共享**资源且有 60s 墙钟：上游一慢，我们每个请求
        # 都白等 60s，然后重试、再白等 60s —— 这不是重试，是**加压**。
        # 实测（2026-10-22）：上游 100% 504 的 30 分钟里，真实用户的
        # 剪藏全部失败。
        #
        # 熔断打开时**立刻失败**（不占上游 60s），让 Arq 退避后重试，
        # 上游才有喘息恢复的窗口。
        if not _circuit_allow():
            log.warning(
                "tts_circuit_open fast_fail provider=%s open_seconds=%s",
                self.base_url,
                _CIRCUIT_OPEN_SECONDS,
            )
            raise _CircuitOpen(
                f"上游 TTS 熔断中（连续失败 {_circuit_state['consecutive_failures']} 次），"
                f"暂停请求 {_CIRCUIT_OPEN_SECONDS:.0f}s"
            )

        log.info(
            "OpenAITTS synthesize: provider=%s model=%s voice=%s bytes=%d "
            "instructions=%r language=%s clone=%s",
            self.base_url,
            self.model,
            body["voice"],
            len(text.encode("utf-8")),
            # 风格 prompt 打出来：段间漂移排查时，第一件事就是确认
            # 「这几段用的到底是不是同一句 instructions」。不打印的话，
            # 配置改了没生效、或某段走了别的分支，日志里看不出来。
            self.instructions or None,
            self.language or None,
            # 克隆也要打：段间漂移排查时，要确认「这几段带的是同一份参考音」。
            "on" if clone else "off",
        )

        try:
            resp = await self._client.post(self._endpoint(), json=body)
        except httpx.TimeoutException as e:
            _circuit_record_failure()
            raise RuntimeError(f"OpenAITTS 超时: {e}") from e
        except httpx.HTTPError as e:
            _circuit_record_failure()
            raise RuntimeError(f"OpenAITTS HTTP 异常: {e}") from e

        if resp.status_code >= 400:
            # 服务端一般会返 JSON 错误体；尝试解析
            err_body: Any
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text[:500]
            # ⚠️ 上游的 60s 墙钟：504 + backend_timeout
            #    「上游语音合成超时 (60s)」
            #    它不是我们的超时，但后果一样（拿不到内容），
            #    所以要单独一个异常类型，让上层走**二分自愈**（更短 → 更不容易撞墙钟）。
            if resp.status_code == 504 and "backend_timeout" in str(err_body):
                _circuit_record_failure()
                raise _UpstreamTimeout(f"上游语音合成超时 (60s): {err_body}")
            # ⚠️ 只有「上游自己扛不住」才算熔断理由：
            #   5xx / 429 = 上游过载或限流 → 继续打只会更糟；
            #   4xx（音色名写错、参数非法）= **我们自己的请求有问题**，
            #   重试一万次也是一样的错，而且它根本不代表上游不可用 ——
            #   把它算进熔断会误伤正常流量。
            if resp.status_code >= 500 or resp.status_code == 429:
                _circuit_record_failure()
            raise RuntimeError(f"OpenAITTS 错误 {resp.status_code}: {err_body}")

        audio = resp.content
        if not audio:
            _circuit_record_failure()
            raise RuntimeError(
                f"OpenAITTS 返回空音频（{resp.status_code}, content-type={resp.headers.get('content-type')}）"
            )

        _circuit_record_success()
        log.info("OpenAITTS synthesized: %d bytes", len(audio))
        return audio

    async def close(self) -> None:
        await self._client.aclose()
