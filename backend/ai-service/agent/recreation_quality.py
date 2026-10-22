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
