"""「再创作」三段内容的质量契约（insights / related / follow_ups）。

## 为什么要有这份契约

播客稿要的不是「原文的压缩」，而是**基于原文的再创作**：
核心要点 + 关联思考 + 延伸判断 + 拓展方向。后三段是稿子里
「原文之外」的部分，也是最容易被写成**标签**的部分。

实测（2026-10-22，生产产出，art_8f52de93146b4c45baf1c80e）：

    insights  「模型能力与执行基建的脱节是散户最大痛点，
               未来竞争焦点将从'谁模型强'转向'谁数据管道更干净'」
              → **复述原文主旨**，不是「原文之外」的思考
    insights  「MCP正在成为AI交易基础设施的USB接口，
               但当前生态仍像早期互联网，标准有了，应用层严重碎片化」
              → ✅ 合格：有主语、有判断、有原文没有的推论
    related   「Bloomberg终端年费近3万美元，QuantGPT本质是AI时代的Bloomberg接口」
              → 只给「是什么」，不给「所以呢」
    related   「Alpaca作为券商API层，是AI交易从研究到执行的关键信任锚点」
              → **定义式标签**
    follow_ups「MCP协议在交易场景下的安全边界设计，如何防止AI被提示注入后误操作」
              → **名词短语**（选题标签），不是给听众的话
    follow_ups「幸存者偏差对量化策略回测的量化影响，
               以及公开数据源中退市公司覆盖率的差异」
              → 同上

6 条里 5 条不合格。肉眼能看出来，但**验收不能靠肉眼** ——
所以这里把「什么算不合格」写成可机械判定的判据。

## 判据（每条都是硬判据，不是启发式打分）

| 码 | 含义 | 机械判定 | 硬缺陷 |
|---|---|---|---|
| `too_short` | 太短，塞不下一个论证 | 字符数 < 40 | ✅ |
| `no_reasoning` | **没有推论**（只是陈述/定义/标签） | 不含任何推理连接词 | ✅ |
| `topic_label` | 名词化的选题短语 | 末尾是「…的设计/的影响/的差异/如何…」等 | ✅ |
| `imperative_start` | 以「关注/研究/对比…」开头（**信号**） | 开头匹配 | ❌ 只记 |
| `restates_body` | 与正文重合度高（**信号**，不是缺陷） | 与正文的字二元组重合度 > 阈值 | ❌ 只记 |
| `no_sentence_end` | 落在「清单模式」（**信号**，不是缺陷） | 末尾不是 。！？ | ❌ 只记 |

`imperative_start` 为什么只算信号：以「关注/研究…」开头只有在**整条都是选题**
时才是毛病；后面跟着三句推理的条目（实测：「关注'数据清洗'对回测真实性的影响。
很多回测漂亮得不真实，根子常在数据里没有退市公司。判断一个AI交易工具，
先看它有没有吃退市的账…」）是**好内容**，只是开头用了「关注」。

`no_reasoning` 是最核心的一条：上面 5 个坏例子**全部**没有任何推理连接词
（因为/所以/但/反而/这意味着/值得/前提/区别在于…），而合格的样本都有。

⚠️ `restates_body` 为什么只算信号（实测踩到过）：

它本来是想抓「把原文主旨换个说法复述一遍」。但它和 prompt **自相矛盾** ——
prompt 要求「推论要能追回原文的某个事实、矛盾或数字」，所以**好条目必然
与正文共享大量词汇**。

实测（2026-10-22，同一篇原文）：

    旧 prompt 的坏样本   二元组重合度 0.154 ~ 0.333
    新 prompt 的好样本   二元组重合度 > 0.34

**重合度与质量反相关** —— 因为新 prompt 写出的条目是「原文的事实 +
新的推论」，而旧的是「原文的词 + 换个说法」。二元组重合度分不开这两者，
所以它只能当信号。

真正抓住「复述」的是 `no_reasoning`：纯复述没有「因为/所以/但」这类推理，
而复述+推论是有的。

⚠️ `no_sentence_end` 为什么只算信号：它其实是**「清单模式」的探针** ——
JSON 数组（`"insights": [...]`）天然把模型切进清单模式，于是这些条目
**全部没有句末标点**，而同一份稿子里的 sections/hook/outro 全都有。
但它本身不是用户抱怨的「不专业、不深」——所以只记，不当缺陷。
（也不要用「给条目补个句号」来刷这个指标：那是自欺。）


## 只记不丢

不合格的条目**不丢弃**（丢内容比内容浅更糟），只记日志 + 计数，
让人能持续盯住 prompt 强化的实际效果。
"""

from __future__ import annotations

import re

# 完整句子的句末标点。标签式条目（选题短语）没有。
_SENTENCE_ENDS = ("。", "！", "？")

# 祈使句 / 选题清单的开头
# ⚠️ 不要把「接下来」算进来 ——
#    prompt 自己的 ✓ 示范就是「接下来真正值得盯的是封闭测试的时间线与反馈」，
#    那是合格写法（带判断）。要抓的是**纯选题**：「关注X」「研究Y」。
_IMPERATIVE_START = re.compile(r"^(关注|研究|对比|看看|留意|追踪|跟踪|不妨|建议|可以多看|值得留意)")

# 末尾是名词化的选题短语（而不是判断）
# ⚠️ 名词化结构必须**锚在尾部**才算标签 ——
#    「QuantGPT的取舍很清楚：…」这种正常句里也有「的取舍」，
#    不锚定会把它误判成标签（实测踩到过）。
_TOPIC_TAIL = re.compile(
    r"(的设计|的影响|的差异|的边界|的机制|的路径|的原理|的逻辑|的演进)$"
    r"|如何[^，。；]{0,24}[?？]?$"
    r"|(是什么|怎么样|为什么|意味着什么)$"
)

# 推理连接词：一条「有深度」的条目必须至少有一个 ——
# 坏样本（定义/标签/复述）全部一个都没有。
#
# ⚠️ 词表要够宽：中文表达推论的方式很多，漏一个就会把好内容误判成标签
# （实测踩到：「…根子常在数据里没有退市公司。判断一个AI交易工具，
#   先看它有没有吃退市的账…」被误判成 no_reasoning，
#   因为词表里只有「因为/所以/但」，没有「根子/先看」）。
_REASONING = re.compile(
    r"(因为|所以|因此|因而|于是|这意味着|意味着|反而|但|却|不同于|区别在于|不等于"
    r"|前提|背后|值得|说明|证明|问题在于|关键在于|根子|根源|源于|来自|取决于"
    r"|如果|一旦|除非|先看|要看|得看|从而|进而|可见|让|使得|导致|意味着"
    # ⚠️ 「不是X，而是Y」：**逗号在「而是」之前**，所以字符类不能排除「，」
    #    （写成 [^，。；] 会漏匹配「往往不是技术，而是…」—— 实测踩到）
    # 「不是A，而是B」和「不是A，是B」都是对比论证（实测两种都出现过）
    r"|不是[^。；]{1,16}[，,][^。；]{0,10}(而)?是"
    # 「不在A，而在B」是标准对比论证（实测漏判过）——
    # 注意这里逗号后面**没有「是」**，所以上一条模式匹配不到
    r"|不在[^。；]{1,16}[，,]\s*而在"
    # ↓ 以下词都是**实测漏判**后补的（每一条都有生产真样本佐证）：
    #   不同 / 印证 / 对比 / 实际上 / 往往是 / 而 / 极贵…
    #   中文表达推论的方式远比英文多，关键词表必然漏 ——
    #   所以这张表是**粗筛**，只抓明显退化，不当「够不够深」的判官。
    r"|不同|印证|对比|实际上|其实是|往往是|而非|并非|极[端高低]|高得|远超"
    r"|当[^，。；]{0,14}时|一旦|只有[^，。；]{0,16}才|除非[^，。；]{0,16}否则"
    # 「这是…的…」在中文里常常是**给理由**（"这是检验…的最直接窗口"），
    # 和「这是X的定义」不同形但同形，只能靠「…」的完整短句来区分
    r"|这正是|原因在于|之所以|好处是|意义在于|价值在于|这是[^。；]{2,20}的[^。；]{0,12}(?=[。！？]?$))"
)

# 具体实体线索：产品/公司/协议名（拉丁串）、数字、引号里的概念
_ENTITY = re.compile(r"[A-Za-z][A-Za-z0-9\.\-]{2,}|\d|「[^」]{2,20}」|《[^》]{2,20}》")

# 与正文重合度阈值（字二元组 Jaccard）。原文主旨被换句话复述时
# 会明显高于这个值；真正的延伸思考则重合度低。
_RESTATE_THRESHOLD = 0.34

# 长度下限。40 字是「塞得下一个论证」的底线 ——
# 生产里 6 条样本的长度是 28~45 字，其中不合格的全在 45 字以下。
# 一个能念出「判断 + 依据」的条目至少要这个长度。
_MIN_ITEM_CHARS = 40


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[^\w一-鿿]+", "", text)
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)} if len(cleaned) > 1 else set()


def overlap_ratio(item: str, body: str) -> float:
    """条目与正文的字二元组重合度（0~1）。"""
    a = _bigrams(item)
    if not a:
        return 0.0
    b = _bigrams(body)
    if not b:
        return 0.0
    return len(a & b) / len(a)


def check_item(item: str, body_text: str = "") -> list[str]:
    """返回一条内容的质量问题码列表（空列表 = 合格）。"""
    issues: list[str] = []
    text = (item or "").strip()
    if not text:
        return ["empty"]
    if len(text) < _MIN_ITEM_CHARS:
        issues.append("too_short")
    if not _REASONING.search(text):
        issues.append("no_reasoning")
    if _TOPIC_TAIL.search(text):
        issues.append("topic_label")
    return issues


def restate_ratio(item: str, body_text: str) -> float:
    """条目与正文的二元组重合度（**信号**，不是判据 —— 见模块 docstring）。"""
    return overlap_ratio(item, body_text)


def check_group(items: list[str], body_text: str = "") -> dict:
    """一批内容的质量画像。

    Returns:
        {"total": n, "ok": n, "issues": {码: 次数}, "signals": {码: 次数},
         "offenders": [条目]}
    """
    offenders: list[str] = []
    issues: dict[str, int] = {}
    signals: dict[str, int] = {}
    ok = 0
    for item in items:
        codes = check_item(item, body_text)
        if not codes:
            ok += 1
        else:
            offenders.append(item)
            for c in codes:
                issues[c] = issues.get(c, 0) + 1
        # 「清单模式」和「与正文重合度高」都只当**信号**记，不进 defects
        if not (item or "").strip().endswith(_SENTENCE_ENDS):
            signals["no_sentence_end"] = signals.get("no_sentence_end", 0) + 1
        if body_text and restate_ratio(item, body_text) > _RESTATE_THRESHOLD:
            signals["restates_body"] = signals.get("restates_body", 0) + 1
        # 「以关注/研究开头」只有在**整条都是选题**时才是毛病；
        # 有推理的条目即使以「关注…」开头也是好内容（实测踩到过）→ 只记信号
        if _IMPERATIVE_START.match((item or "").strip()):
            signals["imperative_start"] = signals.get("imperative_start", 0) + 1
    return {
        "total": len(items),
        "ok": ok,
        "issues": issues,
        "signals": signals,
        "offenders": offenders,
    }


def summarize(groups: dict[str, list[str]], body_text: str = "") -> dict:
    """三组一起出画像：{"insights": {...}, "related": {...}, "follow_ups": {...}}"""
    return {name: check_group(items, body_text) for name, items in groups.items()}


#: 播客稿的**四个必备部分**（2026-10-24 产品拍板）。
#:
#: 产品的定义是：「稿子不该是原文的压缩，而该是
#: **原文核心要点 + 关联思考 + 延伸判断 + 拓展方向**」。
#:
#: ⇒ 四段是稿子的**结构**，不是可选的加分项 —— 缺一段，稿子就不完整。
#: 这条之所以要写成常量，是因为它同时是**判据**（下面）与**文档口径**。
REQUIRED_PARTS: tuple[str, ...] = ("sections", "insights", "related", "follow_ups")


def missing_parts(parts: dict[str, list[str]]) -> list[str]:
    """返回**空缺**的部分名（空列表 = 四段齐备）。

    ## 为什么需要它

    原来的质量检查只判**单条**是否合格（`check_group`），而空组被
    `if not q["total"]: continue` **直接跳过** ⇒
    「延伸判断整段为 0」这件事在日志里**完全静默**。

    后果实测过：文档剪藏与 GitHub 剪藏产出的稿子 `insights` 都是 0，
    而那两篇看上去「挺完整」—— 少了四分之一的内容，没人发现。

    ⇒ 缺段必须**出声**：这是可检测的缺陷，不是「内容质量」问题。
    """
    return [name for name in REQUIRED_PARTS if not (parts.get(name) or [])]


#: 条目里的数字（整数 / 小数 / 百分数）。
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")


def unsourced_numbers(item: str, source_text: str) -> list[str]:
    """条目里出现、但**原文里找不到**的数字。

    ## 为什么需要它

    关联思考 / 延伸判断 / 拓展方向允许写「**原文之外**」的判断 ——
    这正是产品要的东西。但代价是：模型有机会顺手编一个**具体数字**。

    实测（2026-10-24，GitHub README 的稿子）：延伸判断里写了
    「命令数量超过 **20 个**后收益才明显」，而 README 里**根本没有 20**
    （只有 "lazy"）。⇒ 听众会以为那是原文里的事实。

    这类编造**最伤信任**：稿子的其他部分都可以是「我们的看法」，
    但一个看起来像原文事实的数字，是**事实层面的造假**。

    ## 判据（机械、可复现）

    数字在原文里出现过就算有出处。匹配宽松：原文写「3.5」，
    条目写「3.50%」也算对上（只比数字部分，不比百分号，且按数值相等判）。

    ⚠️ 已知误差：**原文被截断**时（抓取侧有长度上限），落在截断处的数字
    会被误判为「没有出处」。所以它只是**信号**，用来告警与抽查，
    不能当判决 —— 定位具体哪一条有问题，仍然要人读一眼。
    """
    if not source_text:
        return []
    # ⚠️ 原文里**一个数字都没有**时，条目里的任何数字都是没出处的
    # （早先这里写成了 `if not src_raw: return []`，等于「原文没数字就放过」——
    #  而那正是最容易编造的具体数字场景：README 里根本没有「20」）。
    src_nums = {_to_num(n) for n in _NUMBER_RE.findall(source_text)}
    out: list[str] = []
    for num in _NUMBER_RE.findall(item or ""):
        if _to_num(num) in src_nums:
            continue
        out.append(num)
    return out


def _to_num(text: str) -> float | None:
    """「3.50%」→ 3.5；解析不了就返回 None（None 永不等于任何数字）。"""
    try:
        return float(text.rstrip("%"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 「丰度」判据（2026-10-25 产品反馈：「内容不够深刻、丰富」）
# ---------------------------------------------------------------------------
#
# ## 为什么「条数」才是杠杆，而不是「每条字数」
#
# 实测（2026-10-25，生产 32 篇 + 两篇新规格探针）：
#
#     段          prompt 规格        生产实测每条字数        生产实测条数/篇
#     sections    80-160 字           均值 123（max 219）      4.1 节
#     related     45-80  字           均值 111（max 147）      1.47 条
#     insights    50-100 字           均值 105（max 145）      2.2 条
#     follow_ups  45-80  字           均值  95（max 124）      1.4 条
#
# ⇒ **每条的字数早就超出规格**（模型自己写得更饱满），真正被卡住的是
# **条数**和**总字数闸门**（prompt 第 10 条把整稿钉在 1000-1400 字）。
# ⇒ 想让内容「更丰富」，该抬的是**条数下限**，不是字数下限。
#
# ## 为什么单条字数下限要**分段**给
#
# 全局 40 字（`_MIN_ITEM_CHARS`）是「塞得下一个论证」的底线。
# 分段的下限取生产实测**最小值**下方一点点（related 72→60、insights 76→70、
# follow_ups 62→60）⇒ 只抓**明显过短**的，不误伤正常条目。
#
# ⚠️ 这两个下限都是「判据」，不是「丢弃规则」：不合格的条目由
# `runner._remediate_recreation` **重写**，不是删掉（删内容比内容浅更糟）。

#: 三段再创作内容的**条目数下限**。
MIN_ITEM_COUNTS: dict[str, int] = {"related": 2, "insights": 3, "follow_ups": 2}

#: 三段再创作内容的**单条字数下限**（分段给，见上面的推导）。
MIN_CHARS_BY_PART: dict[str, int] = {"related": 60, "insights": 70, "follow_ups": 60}


def check_item_for_part(item: str, part: str, body_text: str = "") -> list[str]:
    """按段判定一条内容是否合格。

    = `check_item` + 该段的字数下限（`too_short`）。

    为什么单列一个函数而不是给 `check_item` 加参数：
    `check_item` 是**单条**契约（40 字底线），分段下限是**丰度**契约，
    两者会分头演进（前者跟着「塞得下一个论证」走，后者跟着「说得清一个论证」走）。
    """
    codes = check_item(item, body_text)
    floor = MIN_CHARS_BY_PART.get(part)
    text = (item or "").strip()
    if floor and 0 < len(text) < floor and "too_short" not in codes:
        codes.append("too_short")
    return codes


def weak_items(
    parts: dict[str, list[str]], body_text: str = ""
) -> dict[str, list[tuple[str, list[str]]]]:
    """每段里不合格的条目 + 原因码（供「补救」用）。

    Returns:
        {"related": [(条目, ["no_reasoning"]), ...], ...}；没有不合格的段不出现在结果里。
    """
    out: dict[str, list[tuple[str, list[str]]]] = {}
    for part, items in parts.items():
        bad: list[tuple[str, list[str]]] = []
        for item in items or []:
            codes = check_item_for_part(item, part, body_text)
            if codes:
                bad.append((item, codes))
        if bad:
            out[part] = bad
    return out


def underfilled(parts: dict[str, list[str]]) -> dict[str, str]:
    """未达标的段 → 原因描述（条数不足）。

    与 `missing_parts`（整段为空）互补：这里管的是「段在，但条数不够」。
    """
    out: dict[str, str] = {}
    for part, floor in MIN_ITEM_COUNTS.items():
        items = parts.get(part) or []
        if len(items) < floor:
            out[part] = f"条数 {len(items)} < 下限 {floor}"
    return out


#: 整稿（四段合计）的**目标字数阶梯**，按**原文**长度分档。
#:
#: 为什么要有它（2026-10-25 实测）：prompt 里写了「1900-2600 字」，但模型
#: **不会保证照做** —— 实测一篇 8578 字原文的稿子只写到 1783 字，
#: 比它自己那份规格的下限还低 12%。⇒ 篇幅下限是**软约束**，
#: 想让它成立就得有一条**机械判据**。
#:
#: 三档与 `_REWRITE_SYSTEM` 第 10 条一一对应（改一处要改两处，测试会挡住）。
TARGET_CHARS_BY_SOURCE_LEN: tuple[tuple[int, int, int], ...] = (
    # (原文字数上界, 下限, 上限)
    (500, 900, 1200),
    (2000, 1400, 1900),
    (10**9, 1900, 2600),
)


def target_chars(source_len: int) -> tuple[int, int]:
    """按原文长度返回整稿的 (下限, 上限)。"""
    for upper, low, high in TARGET_CHARS_BY_SOURCE_LEN:
        if source_len <= upper:
            return low, high
    return TARGET_CHARS_BY_SOURCE_LEN[-1][1], TARGET_CHARS_BY_SOURCE_LEN[-1][2]


#: 一条再创作内容的**典型**字数（用于估算「还差几条」）。
#:
#: 取生产实测均值（related 111 / insights 105 / follow_ups 95）的中位偏上值 110。
_TYPICAL_ITEM_CHARS = 110


def chars_shortfall(parts: dict[str, list[str]], source_len: int) -> tuple[int, int]:
    """整稿离字数下限还差多少。

    ⚠️ `parts` 必须包含 **正文要点**（`sections`）——
    prompt 第 10 条的字数阶梯是「sections + insights + related + follow_ups 合计」，
    只算三段的话会拿三段去比四段的下限 ⇒ 差出一个正文的量 ⇒ 补救会被
    触发得莫名其妙（实测会一口气要求补十几条）。

    Returns:
        ``(差额, 还差几条)``；差额 ≤ 0 表示已达下限。
    """
    low, _high = target_chars(source_len)
    total = sum(len(x or "") for items in parts.values() for x in (items or []))
    gap = low - total
    if gap <= 0:
        return 0, 0
    return gap, -(-gap // _TYPICAL_ITEM_CHARS)  # 向上取整
