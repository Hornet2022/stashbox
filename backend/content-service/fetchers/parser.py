"""共享 HTML 解析器（CP2.4 generic_url + CP2.2 wechat 复用）。

CP2.4 在 generic_url.py 里写的 5 个 stdlib HTMLParser，CP2.2 抽到这里让公众号抓取器复用：

| 解析器 | 抽什么 |
|---|---|
| `TitleExtractor` | `<title>` + `og:title`（og:title 优先） |
| `AuthorExtractor` | author → article:author → twitter:creator |
| `TimeExtractor` | article:published_time → pubdate → `<time datetime>` |
| `MediaExtractor` | og:image / og:video + `<img src>`，相对路径 urljoin 转绝对 |
| `ContentExtractor` | readability 简化启发式（密度最高的正文段落） |

外加 3 个工具函数：
- `_norm(text)` → 折叠空白
- `_line_offsets(html)` → 行首偏移表（用于 content_html 真实片段定位）
- `_parse_datetime(raw)` → ISO 8601 / RFC 822 解析

从 generic_url.py 平移时**行为不改**（阈值 / 段落数 / 截断位置全部照搬），
CP2.4 的 5 个解析测试（test_generic_url.py）就是回归验证。

## 正文配图（2026-10-25）

`ContentExtractor` 多了一个**可选**的 `images` 收集器（`media_ocr.MediaImageCollector`）。
传入时，正文里出现的 `<img>` 会在正文里留下一个**不可见占位符**
（`\\x00IMG{n}\\x00`），位置在它在文档里出现的地方 ——
这样后面才能把"图里的文字"**注回原位**，而不是堆在文末。

⚠️ **不传收集器时行为与从前完全一致**（占位符、图片都不出现），
这是刻意的：解析器本身不该改变"正文是什么"，它只负责把位置标出来。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from .media_ocr import MediaImageCollector, pick_image_src

# --------------------------------------------------------------------------
# readability 简化启发式的阈值（每个值的取舍见 fetchers/README.md §5）
# --------------------------------------------------------------------------
MIN_PARAGRAPH_CHARS = 3  # 段落纯文本 < 3 字符直接丢弃（链接/图标）
MIN_TEXT_DENSITY = 0.25  # 纯文本长度 / 原始 HTML 长度下限，低于此判为导航/广告
MAX_PARAGRAPHS = 10  # 最多取密度最高的 10 段
MAX_BLOCK_RAW = 100_000  # 单段原始 HTML 上限（超过整段丢弃）
MAX_BLOCK_TEXT = 20_000  # 单段纯文本上限（超过截断，不算丢弃）
MAX_CONTENT_TEXT = 50 * 1024  # content_text 总上限 50KB（防爆）
MAX_CONTENT_HTML = 200 * 1024  # content_html 总上限 200KB（按整段累加，不切断标签）
MAX_HTML_CHARS = 2 * 1024 * 1024  # 解析前 HTML 截断，防超大页面把内存打爆

#: 整篇正文的最小合理字符数。低于此值**不是**文章，是"没抓到正文"。
#:
#: 为什么需要这条（实测踩到过，不是假想）：密度启发式只看**单段**，
#: 一个 JS 占位页 `<div id="app">loading</div>` 的纯文本/原始长度比是 7/25=0.28，
#: 稳稳高过 `MIN_TEXT_DENSITY` 的 0.25，于是被当成合法正文返回。
#: 结果是剪藏"成功"了，正文只有 7 个字（"loading"），
#: 用户拿到一篇 7 字的音频，而抓取层没有任何异常可报。
#:
#: 这和上一轮修的「`[empty article]` 占位符被当正文喂给 LLM」是同一类故障：
#: **失败伪装成成功**。密度解决不了它，因为壳页里确实有一段"高密度"文本。
#:
#: 取 200 的理由：低于 200 字的网页对"听完一篇文章"这个产品目标没有价值
#: （听 200 字不到 1 分钟），而正常公众号/新闻正文轻松过万字。
#: 宁可对极短公告误报一次（用户看到"没提取到正文"，还能反馈），
#: 也不要让垃圾正文进蒸馏链烧 token。
MIN_ARTICLE_CHARS = 200

# 参与"正文候选"竞争的标签（容器型标签若含嵌套候选段会被判为容器而排除）
_BLOCK_TAGS = frozenset({"p", "div", "article", "section"})
# 这些标签的内容整体跳过（脚本/样式不算正文）
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
# 这些容器里的候选段直接判为样板（导航/页脚/侧栏表单）
_BOILERPLATE_TAGS = frozenset({"nav", "header", "footer", "aside", "form"})

_MEDIA_META_KEYS = frozenset({"og:image", "og:video"})
_AUTHOR_META_KEYS = ("author", "article:author", "twitter:creator")
_TIME_META_KEYS = ("article:published_time", "pubdate")
_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """折叠空白（HTML 里的换行/缩进对我们没意义）。"""
    return _WS_RE.sub(" ", text).strip()


def _line_offsets(html: str) -> list[int]:
    """每行首字符在 html 里的绝对下标，用于把 getpos() 换算成切片下标。"""
    starts = [0]
    pos = html.find("\n")
    while pos != -1:
        starts.append(pos + 1)
        pos = html.find("\n", pos + 1)
    return starts


def _parse_datetime(raw: str) -> datetime | None:
    """解析 ISO 8601 / RFC 822 两种发布时间格式，失败返回 None。

    无时区的按 UTC 处理（FetchResult.publish_time 要求带时区）。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class _Extractor(HTMLParser):
    """5 个解析器的公共基类：feed + close 一步到位，返回 self 方便链式取值。"""

    def parse(self, html: str):  # noqa: ANN201 - 返回 self，子类类型不重要
        self.feed(html)
        self.close()
        return self


class TitleExtractor(_Extractor):
    """抽 <title> 和 <meta property="og:title">（og:title 优先）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_title = ""
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "meta":
            attr = dict(attrs)
            key = (attr.get("property") or attr.get("name") or "").strip().lower()
            if key == "og:title" and not self.og_title:
                self.og_title = (attr.get("content") or "").strip()
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data

    @property
    def best(self) -> str:
        return self.og_title or _norm(self.title)


class AuthorExtractor(_Extractor):
    """按 author → article:author → twitter:creator 顺序取第一个非空值。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._found: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag != "meta":
            return
        attr = dict(attrs)
        key = (attr.get("property") or attr.get("name") or "").strip().lower()
        if key in _AUTHOR_META_KEYS and key not in self._found:
            value = (attr.get("content") or "").strip()
            if value:
                self._found[key] = value

    @property
    def best(self) -> str | None:
        for key in _AUTHOR_META_KEYS:
            value = self._found.get(key)
            if value:
                return _norm(value)
        return None


class TimeExtractor(_Extractor):
    """抽 article:published_time / pubdate meta 和 <time datetime>。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._found: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        attr = dict(attrs)
        if tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").strip().lower()
            if key in _TIME_META_KEYS and key not in self._found:
                value = (attr.get("content") or "").strip()
                if value:
                    self._found[key] = value
        elif tag == "time" and "time" not in self._found:
            value = (attr.get("datetime") or "").strip()
            if value:
                self._found["time"] = value

    @property
    def raw(self) -> str | None:
        for key in (*_TIME_META_KEYS, "time"):
            value = self._found.get(key)
            if value:
                return value
        return None

    @property
    def best(self) -> datetime | None:
        raw = self.raw
        return _parse_datetime(raw) if raw else None


class MediaExtractor(_Extractor):
    """抽 og:image / og:video + <img src>，相对路径用 urljoin 转绝对。"""

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.urls: list[str] = []

    def _add(self, value: str | None) -> None:
        url = (value or "").strip()
        if not url or url.startswith(("data:", "javascript:", "#", "about:")):
            return
        absolute = urljoin(self.base_url, url) if self.base_url else url
        if absolute not in self.urls:
            self.urls.append(absolute)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        attr = dict(attrs)
        if tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").strip().lower()
            if key in _MEDIA_META_KEYS:
                self._add(attr.get("content"))
        elif tag == "img":
            self._add(attr.get("src"))


class ContentExtractor(_Extractor):
    """readability 简化版：密度最高的 <p>/<div>/<article>/<section> 段落聚合。

    密度 = 纯文本长度 / 原始 HTML 长度。只保留"叶子"段（不含嵌套候选段的段），
    容器段（包着正文的 <article>/<div>）不参与竞争，否则会出现整页大段胜出。
    最终取密度最高的 MAX_PARAGRAPHS 段，再按文档顺序输出。

    `images`：可选的正文配图收集器。传入时，正文里的 `<img>` 会留下占位符
    （见模块文档）。**不传就是从前那个行为，一个字都不变。**
    """

    def __init__(self, images: MediaImageCollector | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self._html = ""
        self._line_starts: list[int] = [0]
        self._stack: list[dict[str, Any]] = []
        self._skip_depth = 0
        self._boilerplate_depth = 0
        self.blocks: list[dict[str, Any]] = []
        self._images = images
        # 正文里的图：(占位符, 锚点)。锚点 = 出现这张图时**已经定稿**的块数 - 1，
        # 即"这张图排在哪个块之后"。用它把图插回它在文档里的位置。
        self._image_refs: list[tuple[str, int]] = []

    # -- 位置换算 ---------------------------------------------------------
    def _abs(self) -> int:
        lineno, offset = self.getpos()
        idx = lineno - 1
        if idx >= len(self._line_starts):
            return len(self._html)
        return self._line_starts[idx] + offset

    def _abs_end(self) -> int:
        """结束标签 `</tag>` 之后的下标。"""
        start = self._abs()
        gt = self._html.find(">", start)
        return gt + 1 if gt != -1 else len(self._html)

    def parse(self, html: str):  # noqa: ANN201
        self._html = html
        self._line_starts = _line_offsets(html)
        return super().parse(html)

    # -- 解析 -------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "img":
            self._collect_image(dict(attrs))
            return
        if tag in _BOILERPLATE_TAGS:
            self._boilerplate_depth += 1
        if tag in _BLOCK_TAGS:
            self._stack.append(
                {
                    "tag": tag,
                    "start": self._abs(),
                    "text": [],
                    "boilerplate": self._boilerplate_depth > 0,
                    "has_child": False,
                }
            )

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _BOILERPLATE_TAGS:
            self._boilerplate_depth = max(0, self._boilerplate_depth - 1)
        # 从栈顶往下找同名标签（容忍 <div><p></div> 这种错配写法）
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                while len(self._stack) > i:
                    frame = self._stack.pop()
                    if self._stack:
                        self._stack[-1]["has_child"] = True
                    self._finalize(frame)
                break

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._stack:
            return
        self._stack[-1]["text"].append(data)

    def _collect_image(self, attr: dict[str, str]) -> None:
        """正文里的一张 `<img>` → 登记占位符 + 记住它在文档里的位置。

        位置用"锚点"表示：**这张图排在已经定稿的第 N 个块之后**。
        段落是边解析边定稿的，所以锚点就是"此刻已定稿的块数 - 1"。

        图在某个块**里面**时先挂在那个块上：等这个块定稿后，
        锚点会改成它自己的序号（图注就落在这一段之后，位置最准）。
        """
        if self._images is None:
            return
        token = self._images.add(pick_image_src(attr))
        if not token:
            return
        anchor = len(self.blocks) - 1
        if self._stack:
            self._stack[-1].setdefault("images", []).append((token, anchor))
        else:
            self._image_refs.append((token, anchor))

    def _finalize(self, frame: dict[str, Any]) -> None:
        raw = self._html[frame["start"] : self._abs_end()]
        text = _norm("".join(frame["text"]))[:MAX_BLOCK_TEXT]
        images = frame.get("images") or []
        if frame["boilerplate"] or frame["has_child"]:
            # 这个块被丢掉（容器/样板）⇒ 里面的图回到"松散图"，
            # 锚点保持"出现时已定稿的块数 - 1"，位置仍然对。
            self._image_refs.extend(images)
            return
        if len(text) < MIN_PARAGRAPH_CHARS or len(raw) > MAX_BLOCK_RAW:
            self._image_refs.extend(images)
            return
        density = len(text) / max(len(raw), 1)
        if density < MIN_TEXT_DENSITY:
            self._image_refs.extend(images)
            return
        self.blocks.append(
            {
                "order": len(self.blocks),
                "tag": frame["tag"],
                "raw": raw,
                "text": text,
                "density": density,
            }
        )
        # 块被选中 ⇒ 里面的图锚到这个块（= 紧跟这一段之后）
        order = len(self.blocks) - 1
        self._image_refs.extend((token, order) for token, _ in images)

    # -- 输出 -------------------------------------------------------------
    def best(self, limit: int = MAX_PARAGRAPHS) -> list[dict[str, Any]]:
        """密度最高的 limit 段，按文档顺序返回。"""
        ranked = sorted(self.blocks, key=lambda b: (-b["density"], b["order"]))[:limit]
        return sorted(ranked, key=lambda b: b["order"])

    def content_text(self, limit: int = MAX_PARAGRAPHS) -> str:
        blocks = self.best(limit)
        if not self._image_refs:
            return "\n\n".join(b["text"] for b in blocks)[:MAX_CONTENT_TEXT]
        return "\n\n".join(self._parts(blocks))[:MAX_CONTENT_TEXT]

    def _parts(self, blocks: list[dict[str, Any]]) -> list[str]:
        """正文段落 + 图片占位符，按文档顺序拼成一份。

        图片占位符**单独成段**（和段落之间用空行隔开）：
        这样图注替换进去之后是"图表：……"独立一行，
        不会和段落文字粘在一起（下游 prompt 禁止括号/方括号类舞台提示，
        图注必须读起来像正文的一部分）。

        锚点规则：锚点 A = "这张图排在第 A 个块之后"。
        - 锚点落在某个**被选中**的块上 → 紧跟那个块；
        - 锚点落在一个**被丢掉**的块上 → 跟在它前面那个被选中的块后面
          （文档位置不会因为密度启发式丢块而改变）；
        - 锚点在第一个被选中的块之前 → 排在最前面。
        """
        pending = sorted(self._image_refs, key=lambda item: item[1])
        parts: list[str] = []
        idx = 0
        for block in blocks:
            # 锚点 < 当前块 → 这些图在当前块之前（前面没有可挂靠的选中块）
            while idx < len(pending) and pending[idx][1] < block["order"]:
                parts.append(pending[idx][0])
                idx += 1
            parts.append(block["text"])
            # 锚点 == 当前块 → 紧跟这个块之后
            while idx < len(pending) and pending[idx][1] == block["order"]:
                parts.append(pending[idx][0])
                idx += 1
        # 剩下的图排在最后一个选中块之后
        while idx < len(pending):
            parts.append(pending[idx][0])
            idx += 1
        return parts

    def content_html(self, limit: int = MAX_PARAGRAPHS) -> str:
        out: list[str] = []
        total = 0
        for block in self.best(limit):
            if total + len(block["raw"]) > MAX_CONTENT_HTML:
                break
            out.append(block["raw"])
            total += len(block["raw"])
        return "\n".join(out)


__all__ = [
    "AuthorExtractor",
    "ContentExtractor",
    "MAX_HTML_CHARS",
    "MAX_PARAGRAPHS",
    "MIN_PARAGRAPH_CHARS",
    "MIN_TEXT_DENSITY",
    "MIN_ARTICLE_CHARS",
    "MediaExtractor",
    "TimeExtractor",
    "TitleExtractor",
]
