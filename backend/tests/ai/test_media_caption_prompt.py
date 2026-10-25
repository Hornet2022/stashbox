"""下游 prompt 对「图表：」的口径（2026-10-25）。

## 为什么要有这个文件

配图识图上线后，正文里会开始出现「图表：xxx」这种文字
（`content-service/fetchers/media_ocr.py` 把图里的字读出来、注回原位）。

而 rewrite prompt 里**原本一个字都没提它** —— 模型只能靠猜。

实测一篇 14 张图的公众号文章，14 条图注的内容分布是：

    图 1  概览图 + 对比表（个人提效 vs 组织提效的四个断点）  → 有信息
    图 2  「落地前的 15 个问题」清单                          → 有信息
    图 3  标题图「百万级 FDE 项目钱到底花在哪」              → 弱
    图 4  四阶段流程图（诊断→试点→工程化→验收交接）          → 有信息
    图 5  「要定清的事」清单                                  → 有信息
    图 6  一句话要点                                          → 弱
    图 7~13 装饰插画的**纯画面描述**（「一个男孩正在操作一台机器」）→ 零信息
    图 14 作者名片（「Miles AI算法专家 × FDE … 关注我」）      → 零信息

⇒ 不给口径，图 7~14 有可能进稿子；给错口径（照单全收），
稿子会被「一个男孩在操作机器」这种句子拉低。

## ⚠️ 实测把上面的推断修正了（2026-10-25）

同一份 12 条图注的正文，旧 prompt / 新 prompt 各跑 2 次：

    旧 prompt: 2638 / 2318 字   装饰命中 0/2
    新 prompt: 2889 / 2378 字   装饰命中 0/2

⇒ **旧 prompt 也没有把装饰描述写进稿子**（模型自己会忽略）。
   本条口径**不是"修污染"的补丁**，它买的是**确定性**：
   把「这是配图里的字、画面描述要丢、不确定就丢」的**默认动作**定死，
   而不是赌模型每次都恰好忽略。

## 本文件锁住的口径

1. prompt 必须**认出**「图表：」是什么（配图里读出来的文字，是素材不是正文）；
2. prompt 必须**给出取舍判据**（对读者有没有新增信息），而不只是「你可以用」；
3. prompt 必须**明确说画面描述/装饰要丢**（否则模型倾向于「有总比没有好」地照搬）。
"""

import sys
from pathlib import Path

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
AI_SERVICE = Path(__file__).resolve().parents[2] / "ai-service"
if str(AI_SERVICE) not in sys.path:
    sys.path.insert(0, str(AI_SERVICE))

import agent.runner as runner_mod  # noqa: E402


def test_prompt认识图表前缀():
    """prompt 必须说清「图表：」是什么 —— 不然模型只能靠猜。"""
    p = runner_mod._REWRITE_SYSTEM
    assert "图表：" in p, "prompt 里没有提「图表：」—— 模型无从知道那是什么"


def test_prompt说清图表是素材不是正文():
    assert "配图里读出来的文字" in runner_mod._REWRITE_SYSTEM
    assert "素材" in runner_mod._REWRITE_SYSTEM


def test_prompt给出取舍判据():
    """只说「可以用」不够 —— 必须给判据，否则模型倾向于照单全收。"""
    p = runner_mod._REWRITE_SYSTEM
    assert "新增信息" in p, "没给「要不要用」的判据"
    assert "不确定就丢" in p, "必须给出不确定时的默认动作（丢掉，而不是塞进去）"


def test_prompt明确要求丢掉画面描述与装饰():
    """装饰插画的 OCR 结果是「一个男孩正在操作一台机器」这种画面描述 ——
    不明确说丢，稿子里就会出现这种与主题无关的句子。"""
    p = runner_mod._REWRITE_SYSTEM
    assert "画面描述" in p
    assert "直接忽略" in p


def test_规则编号不重复():
    """「写作原则」这一组的编号必须连续且不重复。

    ⚠️ 只查**这一组**：prompt 里有好几组编号列表（标签规则 1-3、
    写作原则 1-N），跨组编号重复是正常的。
    模型会照着「第 N 条」理解，组内编号乱了它就会指错。
    """
    p = runner_mod._REWRITE_SYSTEM
    section = p.split("【写作原则】", 1)[1].split("输出纯 JSON", 1)[0]
    numbers = [
        int(line.strip().split(".", 1)[0])
        for line in section.splitlines()
        if line.strip()[:1].isdigit() and "." in line.strip()[:5]
    ]
    assert numbers, "没解析出任何规则编号 —— 解析逻辑该跟着 prompt 格式一起改"
    assert numbers == list(range(1, len(numbers) + 1)), f"写作原则的编号不连续或有重复：{numbers}"


def test_内容服务侧的图注前缀与prompt口径对得上():
    """⚠️ **两处必须对得上**：识别正文用的是一个前缀，prompt 里教模型认的
    必须是**同一个**前缀。改了 content-service 那边的常量却没改 prompt，
    模型就会认不出（图注全变成噪音）。

    这是「同一个事实只有一份定义」的典型：前缀本身是契约。
    """
    content_dir = Path(__file__).resolve().parents[2] / "content-service"
    if str(content_dir) not in sys.path:
        sys.path.insert(0, str(content_dir))
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "cs_caption_prefix_fetchers",
        content_dir / "fetchers" / "__init__.py",
        submodule_search_locations=[str(content_dir / "fetchers")],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cs_caption_prefix_fetchers"] = mod
    spec.loader.exec_module(mod)
    media_ocr = importlib.import_module("cs_caption_prefix_fetchers.media_ocr")

    assert media_ocr.CAPTION_PREFIX.strip() == "图表：", (
        f"图注前缀改成了 {media_ocr.CAPTION_PREFIX!r}，"
        "但 rewrite prompt 里教模型认的还是「图表：」—— 两处要对上"
    )
    assert media_ocr.CAPTION_PREFIX.strip() in runner_mod._REWRITE_SYSTEM
