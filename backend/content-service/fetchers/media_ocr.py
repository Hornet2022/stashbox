"""正文配图识图（2026-10-25）。

## 为什么要有它（问题不是"锦上添花"，是数据在链路里被物理丢掉）

剪藏一个链接时，网页正文里的**图表 / 配图**会被**整个丢掉**：

- `fetchers/wechat.py` / `parser.py` 把 HTML 文本化时只收文本节点
  （`wechat.py` 的 `_TagStripper.handle_data`），而 `<img>` **没有文本节点**
  ⇒ 图在 `content_text` 里**物理消失**；
- 图片 URL 其实抓到了（`FetchResult.media_urls`），但**下游没人读**；
- `ai-service/tasks/distill_task.py` 只取 `raw_content["content_text"]`
  ⇒ 图表里的数字在蒸馏之前就已经没了。

结果是一篇"用图表讲数据"的文章，听出来只有干巴巴的叙述，
**图表里的数字一个都没进稿子** —— 而这恰恰是这类文章的全部信息量。

## 做法：占位符 → 识图 → 原位注入

1. 文本化时，每遇到**正文里**的一个 `<img>`，在正文里留一个**不可见占位符**
   `\\x00IMG{n}\\x00`（N 从 1 递增）；
2. 抓完正文后，把这些图**下载 + 识图**（复用 `fetchers/ocr.py` 的识图通道，
   不复制请求体拼装）；
3. 把占位符**替换成图注文字**；失败的**直接删掉**，不留任何残迹。

### 为什么用 NUL 哨兵做占位符

正文里出现 `[[IMG1]]` 这种串是**可能的**（用户自己就写了），
而 `\\x00` 在正常文本里不会出现 ⇒ 不会和真实文字碰撞；
更重要的是它**一定**会被替换或删除 —— 交给 LLM 的正文里不可能留着它
（`caption_media_into` 收尾还会再扫一遍）。

## 边界（明确的取舍，不是"以后再说"）

* **只给正文里的图留占位符**：`og:image` 是 `<meta>` 不是 `<img>`，
  天然进不了正文；封面/头图同理（判断依据就是"解析正文 HTML 时拿到的
  `<img src>` / `<img data-src>`"）；
* **每篇最多识 `MAX_MEDIA_CAPTIONS` 张图** —— 识图是**真金白银**的 token，
  而一篇长文可能有几十张图；上限的取值与实测依据见该常量的注释；
* **下载并发（≤4）、识图并发（≤3，全进程 ≤6）** —— 实测 provider 在并发 6 下
  零失败零限流（详见 `MEDIA_OCR_CONCURRENCY` 的注释），
  而串行会让剪藏从 ~10s 变成 22.8s；
* **下载与识图走有界队列流水线** —— 两者重叠（总耗时 ≈ max 而非相加），
  且内存里的图片张数有闸门（见 `MEDIA_OCR_QUEUE_SIZE`）；
* **失败不阻断**：下载失败 / 超时 / 识图失败 / 图注为空
  ⇒ 该图 `ok=False`，正文里对应的占位符**直接删掉**，
  原因记进 `raw_metadata["media_captions"]`
  （"这篇为什么没有图表信息"查得到原因）；
* **整段流程的任何异常都不许让抓取失败**（见 `caption_media_into`）：
  配图是锦上添花，正文抓不到才是硬伤。
"""

from __future__ import annotations

import asyncio
import logging
import re
import weakref
from typing import TypedDict
from urllib.parse import urljoin

import httpx

from .base import FetchResult
from .net import (
    Deadline,
    HeaderProfile,
    HOST_THROTTLE,
    acquire_client,
    build_client_kwargs,
    host_of,
)
from .ocr import image_mime_for, ocr_config, ocr_image_bytes

log = logging.getLogger("stashbox.fetch.media")


#: 每篇最多识几张图。
#:
#: ## 为什么从 5 提到 12（2026-10-25 实测）
#:
#: 旧值 5 的理由是"识图是真金白银的 token，再多就是烧钱换边际信息"。
#: 这个理由**方向对，但归因错了** —— 真正的约束不是 token，是**时间**：
#!
#: 实测（生产 8.148.9.151，14 张正文图的公众号文章）：
#!   · 单张 OCR 均值 3.4s（范围 2.3~5.7s），**串行** ⇒ 5 张 = 17s；
#:   · 下载 14 张（并发 4）只要 4.0s；
#!   · 现状（串行 + 30s 预算）实测整篇 22.8s，**只能识 5 张**。
#!
#: 而把 OCR 改成并发 3 之后（见 `MEDIA_OCR_CONCURRENCY`）：
#!   · 14 张全识完只要 17.4s（provider 实测零失败、零限流）；
#:   · ⇒ **条数上限才是那个真正卡人的旋钮**，时间已经不是了。
#:
#: 12 是「覆盖绝大多数文章的图数」与「token 成本」之间的折中：
#: 生产 33 篇里 22 篇（67%）图数 > 5，上限 5 让**三分之二的文章白白丢了图**。
#:
#: ⚠️ **上限不是「越多越好」的承诺**（2026-10-25 实测）：
#: 多识出来的图里有很多是装饰插画 —— 实测一篇 14 图的文章，图 7~13 的 OCR
#: 结果是"一个男孩正在操作一台机器"这种纯画面描述，图 14 是作者名片。
#:
#: 但**实测它对稿子没有可测影响**：把上限从 5 提到 12（正文里多 7 条图注、
#: 多 700 字噪音）后，同一篇的稿子 2167 → 2144 字，装饰描述**一条都没进稿子**
#: （新旧 prompt 各 2 次调用，8 次里 0 次命中）。
#:
#: ⇒ 提上限的理由是**覆盖率**，不是"稿子更好"：
#: 生产 33 篇里 22 篇（67%）图数 > 5，而旧上限 5 还会让
#: 「前 5 张全是图标」的文章（实测 sspai 一篇）**一张都拿不到**。
#: ⇒ 代价是 token 成本线性上涨（识 N 张 ≈ N 份 VLM token）。
#:
#: 上限最终定多少由 `tests/content/test_media_ocr.py` 里的判据锁住，
#: 不是拍脑袋 —— 改这个数之前先跑一次同口径的产出对照。
MAX_MEDIA_CAPTIONS = 12

#: 单图下载超时（秒）。
MEDIA_DOWNLOAD_TIMEOUT = 15.0

#: 单图识图超时（秒）。
#:
#: 比扫描件 OCR 的 `ocr.OCR_TIMEOUT`（90s）短：单张图认不出来就丢掉它，
#: 不值得为它把整篇的等待时间拖长。
MEDIA_OCR_TIMEOUT = 15.0

#: 单图大小上限（字节）。超了直接跳过，别把内存吃穿。
MAX_MEDIA_BYTES = 5 * 1024 * 1024

#: 图片下载并发上限。
MEDIA_DOWNLOAD_CONCURRENCY = 4

#: 单篇的**识图并发**上限。
#:
#: ## 为什么要并发（2026-10-25 实测）
#:
#: 原来是**串行**的，理由写的是"provider 侧要限流、也要省钱"。实测下来：
#! 同一篇 14 张图，只改并发度（同一批图、同一 provider）：
#!
#:     并发 1 → OCR 阶段 49.3s      并发 4 → 14.1s
#:     并发 2 → 26.4s               并发 6 → 11.0s
#!     并发 3 → 17.4s
#!
#: **全部 14/14 成功，零限流、零失败** ⇒ 串行不是 provider 的硬约束，
#: 只是一个**保守的默认**。串行的代价是实打实的：剪藏从 ~10s 变成 22.8s。
#!
#: 取 3 而不是 6：并发越高，provider 侧的瞬时压力越大，
#! 而剪藏是**同步接口**、用户盯着转圈 —— 3 已经能让「识完 12 张」落进 30s 预算。
MEDIA_OCR_CONCURRENCY = 3

#: **全进程**同时在跑的识图请求数上限。
#:
#: 单篇并发是「一篇之内」的闸门；这个是「整台机器」的闸门 ——
#: 多个用户同时剪藏时，provider 侧看到的并发是 `单篇并发 × 同时剪藏数`。
#: 内测阶段同时剪藏的人不多，但**万一**来了 5 个人，就是 15 个并发打过去。
#:
#: 跨 loop 安全：信号量按 **event loop** 分开存（见 `_global_ocr_semaphore`），
#: 因为 `asyncio.Semaphore` 会绑定 loop，而测试里每个用例一个新 loop。
MEDIA_OCR_GLOBAL_CONCURRENCY = 6

#: 下载→识图之间那道**有界队列**的长度。
#:
#: 它是**内存的闸门**：队列满了，生产者（下载）就得等着 ⇒
#: 内存里待识图的图片张数有上限，而不是「N 张 5MB 全堆在内存里」
#: （服务 MemoryMax=176M，12 张 5MB = 60MB，不设闸门就贴着上限跑）。
MEDIA_OCR_QUEUE_SIZE = 3

#: 全进程识图信号量，**按 event loop** 分开存（理由见 `_global_ocr_semaphore`）。
_GLOBAL_OCR_SEMAPHORES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

#: 图注前缀（**普通文字**）。
#:
#: 刻意不用括号/方括号：下游 prompt 明确禁止 `（轻松开场音乐淡出）` 这类
#: 舞台提示，方括号 `【】` 也可能被误当成音效标注 —— 正文里冒出来
#: 一句"图表：xxx"才是安全的。
CAPTION_PREFIX = "图表："

#: 占位符格式：NUL 哨兵 + 序号 + NUL 哨兵。
TOKEN_RE = re.compile("\x00IMG(\\d+)\x00")

#: 识图提示词（**单独一版**：图表不是纯文字段落）。
#:
#: 扫描件那版是"识别图片里的全部文字"，对图表会得到一堆没顺序的碎片
#: （先横轴再图例还是先图例再横轴全看模型心情）。
#: 这版明确要求**按阅读顺序**整理成通顺短句，且**只输出内容本身**。
_MEDIA_OCR_PROMPT = (
    "这张图可能是一张图表。请把图里的文字与数字按阅读顺序整理成通顺的短句输出；"
    "只输出内容本身，不要输出任何标记、解释或 markdown。"
)


class ImageRef(TypedDict):
    """正文里的一张图（占位符 + 真链）。"""

    url: str
    token: str


class MediaCaption(TypedDict):
    """一张图的识图结果。

    `ok=False` 时 `caption` 为空串、`error` 说明原因 ——
    这份记录会进 `raw_metadata["media_captions"]`，
    用来回答"这篇为什么没有图表信息"。
    """

    url: str
    token: str
    caption: str
    ok: bool
    error: str | None


def make_token(index: int) -> str:
    """第 index 张图的占位符（index 从 1 开始）。"""
    return f"\x00IMG{index}\x00"


def token_index(token: str) -> int | None:
    """占位符 → 序号；不是占位符返回 None。"""
    match = TOKEN_RE.fullmatch(token)
    return int(match.group(1)) if match else None


def json_safe_token(token: str) -> str:
    """占位符 → **可落库**形态（NUL 换成可见的 ``\\x00`` 记法）。

    为什么必须有这一步：占位符里带 NUL 哨兵（正常文字里不会出现，
    所以不会和真实内容碰撞），而 **PostgreSQL 的 JSONB 不接受** ``\\u0000``
    （实测报 ``unsupported Unicode escape sequence``）——
    带 NUL 的记录一旦落库，整条建库就炸在那里。

    ⇒ 内存里用 NUL 哨兵，落库前换成同名可见记法（可逆、信息不丢）。
    """
    return token.replace("\x00", "\\x00")


def refs_from_storage(items: list[dict]) -> list[ImageRef]:
    """落库形态的图清单 → 内存里的 `ImageRef`（还原带 NUL 的占位符）。"""
    return [
        {"url": str(item.get("url", "")), "token": make_token(int(item.get("index", 0)))}
        for item in items
    ]


def pick_image_src(attrs: dict[str, str]) -> str:
    """取图片真链。

    公众号默认懒加载，真链在 `data-src`（`src` 常常是个 base64 占位图）；
    普通网页通常在 `src`，但也有懒加载站把真链放 `data-src`。
    ⇒ **先看 `data-src`，没有再看 `src`**。

    `data:` / `javascript:` / `#` 这类不是可下载的 URL，直接当没有。
    """
    for key in ("data-src", "src"):
        value = (attrs.get(key) or "").strip()
        if value and not value.startswith(("data:", "javascript:", "#", "about:")):
            return value
    return ""


class MediaImageCollector:
    """收集正文里的图，并给每张图发一个占位符。

    为什么单独一个类：占位符的**编号**必须和"这张图的 URL"严格对应，
    而它们分别产生在两个地方（文本化时发号、下载时要 URL）。
    把它们绑在一个对象里，就不可能出现"正文里有占位符但没有对应 URL"
    （那会让占位符永远留在正文里）。
    """

    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url
        self._refs: list[ImageRef] = []

    def add(self, raw_src: str) -> str:
        """登记一张图，返回它的占位符；不是可用图片时返回空串（不发占位符）。"""
        src = (raw_src or "").strip()
        if not src:
            return ""
        url = urljoin(self.base_url, src) if self.base_url else src
        if not url.startswith(("http://", "https://")):
            # 相对路径没 base_url 兜底时可能拼出奇怪的东西；非 http(s) 一律不发占位符
            return ""
        token = make_token(len(self._refs) + 1)
        self._refs.append({"url": url, "token": token})
        return token

    @property
    def refs(self) -> list[ImageRef]:
        """已登记的图（按出现顺序）。"""
        return list(self._refs)

    @property
    def storage_refs(self) -> list[dict]:
        """**可落库**的图清单（只有 url + 序号，没有带 NUL 的占位符）。

        为什么不让占位符进 `raw_metadata`：那是 JSONB，PG 不接受 ``\\u0000``
        （见 `json_safe_token`）。序号足够还原占位符（`make_token(index)`），
        所以这里只存序号。
        """
        return [{"url": ref["url"], "index": token_index(ref["token"]) or 0} for ref in self._refs]


def _failed(ref: ImageRef, error: str) -> MediaCaption:
    """一张失败的图（占位符会被直接删掉，原因记下来）。"""
    return {
        "url": ref.get("url", ""),
        "token": ref.get("token", ""),
        "caption": "",
        "ok": False,
        "error": error[:200],
    }


async def _download_one(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> tuple[bytes | None, str | None, str | None]:
    """下载一张图 → (图片字节, mime, 错误原因)。

    失败**不抛异常**：一张图坏掉不该让整篇抓取失败（见模块文档的"失败不阻断"）。

    ⚠️ `headers` 必须走**请求级**（httpx 里请求级覆盖 client 级），
    理由见 `caption_media` 里 Referer 那段注释 —— 放进 client 级会被
    client 池吞掉（池的 key 不含 headers，首个使用者的值会被"烧"进去）。
    """
    blob = b""
    try:
        # 按 host 节流：我们对同一个图床连打好几张图，很容易被当成爬虫。
        await HOST_THROTTLE.wait(host_of(url))
        async with client.stream("GET", url, timeout=timeout, headers=headers) as resp:
            if resp.status_code != 200:
                return None, None, f"下载失败: http {resp.status_code}"
            length = resp.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_MEDIA_BYTES:
                # 先看 Content-Length：别把 50MB 的图先读进内存再判断。
                return None, None, f"图片过大（{int(length)} 字节 > {MAX_MEDIA_BYTES}）"
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    return None, None, f"图片过大（超过 {MAX_MEDIA_BYTES} 字节）"
                chunks.append(chunk)
            blob = b"".join(chunks)
    except httpx.HTTPError as exc:
        return None, None, f"下载失败: {type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001 —— 一张图的任何异常都不该炸掉整篇
        return None, None, f"下载失败: {type(exc).__name__}: {exc}"

    if not blob:
        return None, None, "下载失败: 空响应"
    mime = image_mime_for(blob)
    if mime is None:
        # 常见于图床返回了 HTML 错误页（防盗链/404）。按魔数判断，
        # 别把一段 HTML 塞给识图模型（既浪费 token 又会得到胡说）。
        return None, None, "下载失败: 不是图片（按魔数判断）"
    return blob, mime, None


def _global_ocr_semaphore() -> asyncio.Semaphore:
    """全进程同时在跑的识图请求数上限（见 `MEDIA_OCR_GLOBAL_CONCURRENCY`）。

    ## 为什么不能是模块级单例

    `asyncio.Semaphore` 通过 `_LoopBoundMixin` **绑定第一次 await 它的那个 loop**，
    换到别的 loop 再用就抛 `is bound to a different event loop`。
    而 pytest-asyncio **每个用例一个新 loop** ⇒ 模块级单例在第二个用例里必炸。

    ⇒ 按 loop 分别存；用 `WeakKeyDictionary` 是为了 loop 回收时信号量也跟着走
    （否则测试跑久了会攒下一堆没人用的信号量）。
    """
    loop = asyncio.get_running_loop()
    sem = _GLOBAL_OCR_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(MEDIA_OCR_GLOBAL_CONCURRENCY)
        _GLOBAL_OCR_SEMAPHORES[loop] = sem
    return sem


async def caption_media(
    images: list[ImageRef],
    *,
    referer: str | None = None,
    limit: int = MAX_MEDIA_CAPTIONS,
    profile: HeaderProfile = HeaderProfile.DESKTOP,
    transport: httpx.AsyncBaseTransport | None = None,
    deadline: Deadline | None = None,
) -> list[MediaCaption]:
    """给正文里的图识图 → 每张图一条结果（成功/失败都有记录）。

    Args:
        images: 正文里的图（占位符 + 真链）。
        referer: 防盗链用的 Referer（微信图床 `mmbiz.qpic.cn` 认这个）。
        limit: 最多识几张（超出的直接判失败，占位符会被删掉）。
        profile: 下载用的 header 画像。
        transport: 测试注入点（httpx.MockTransport）；生产传 None（走 SSRF 防护）。

    Returns:
        与 `images` **一一对应**、顺序相同的列表。
    """
    if not images:
        return []

    attempted = list(images[:limit])
    skipped = list(images[limit:])
    results: dict[str, MediaCaption] = {}

    for ref in skipped:
        results[ref["token"]] = _failed(ref, f"超出单篇识图上限（最多 {limit} 张）")

    # 早退：识图通道不可用就**别下载图**（白下载一遍，最后还是识不了，
    # 还把流量和图床的耐心都耗掉了）。
    base_url, api_key, _model = await ocr_config()
    if not base_url or not api_key:
        for ref in attempted:
            results[ref["token"]] = _failed(ref, "识图通道不可用（没配 LLM provider）")
        return _ordered(images, results)

    # ⚠️ Referer 必须走**请求级**，不能进 client 级 headers ——
    #
    # 2026-10-25 实测的真 bug：client 是**池化**的，而池的 key 是
    # `(画像, 代理, guard_ssrf)` —— **不含 headers**（见 net.acquire_client）。
    # ⇒ 第一次用某画像建 client 时，它当时的 headers 被"烧"进 client 级，
    #   之后同画像的请求**复用**它 ⇒ **新传的 extra_headers 被丢弃**。
    #
    # 于是：页面抓取（`build_client_kwargs(profile)`，不带 Referer）先建了
    # DESKTOP client ⇒ 识图时传的 `Referer: <文章URL>` **必然丢失** ⇒
    # 图床防盗链回 403（实测 sspai 47 张全 403；用独立 client 手工测就是 200）。
    #
    # 微信为什么没事：它的 Referer 在**画像**里（`https://mp.weixin.qq.com/`），
    # 那是 client 级的、固定的 ⇒ 不会被吞。⇒ **只有 per-article 的 Referer 会中招**。
    #
    # ⇒ 修法：Referer 每次请求都带（httpx 里请求级 headers 覆盖 client 级）。
    request_headers = {"Referer": referer} if referer else None
    client_kwargs = build_client_kwargs(
        profile,
        # 注入了 MockTransport 就不跑 DNS 校验：mock 不发真实请求，
        # 跑校验只会让用例依赖外网。生产路径永远带防护（与其它 fetcher 同一套约定）。
        guard_ssrf=transport is None,
        transport=transport,
    )
    client = acquire_client(profile, **client_kwargs)
    semaphore = asyncio.Semaphore(MEDIA_DOWNLOAD_CONCURRENCY)

    # ── 下载 → 识图：**有界队列流水线** ─────────────────────────────────
    #
    # 为什么不是「全部下完再开始识」（原来的写法）：
    #   ① 慢：下载的 4s 和识图的 17s **串行相加**；
    #   ② 内存：N 张图（每张上限 5MB）**同时**堆在内存里
    #      —— 12 张就是 60MB，而服务 MemoryMax=176M。
    #
    # 流水线：下载完的图**立刻**进队列等识图，队列有界 ⇒ 队列满时下载得停下
    # ⇒ ① 下载与识图重叠（总耗时 ≈ max(下载, 识图)）；
    #   ② 内存里的图片张数有闸门（队列长度 + 在识的并发数）。
    queue: asyncio.Queue = asyncio.Queue(maxsize=MEDIA_OCR_QUEUE_SIZE)

    async def _download(ref: ImageRef) -> tuple[bytes | None, str | None, str | None]:
        # ⚠️ 超时取 **抓取总预算的剩余**，不是固定的 15s：
        # 剪藏是同步接口，用户盯着转圈等，总时限（30s）是硬约束（见 net.Deadline）。
        # 抓取本身可能已经用掉 25s ⇒ 留给识图的只有 5s ⇒ 下载必须自己收敛。
        timeout = MEDIA_DOWNLOAD_TIMEOUT
        if deadline is not None:
            if deadline.expired():
                return None, None, "抓取预算已耗尽（没时间下载配图了）"
            timeout = deadline.slice(MEDIA_DOWNLOAD_TIMEOUT) or MEDIA_DOWNLOAD_TIMEOUT
        async with semaphore:
            return await _download_one(client, ref["url"], timeout=timeout, headers=request_headers)

    async def _producer(ref: ImageRef) -> None:
        """下载一张图 → 放进队列。**任何异常都要进队列**（否则消费者会等一辈子）。"""
        try:
            blob, mime, error = await _download(ref)
        except Exception as exc:  # noqa: BLE001 —— 一张图下载炸了不该拖死整篇
            blob, mime, error = None, None, f"下载失败: {type(exc).__name__}: {exc}"
        await queue.put((ref, blob, mime, error))

    async def _consumer() -> None:
        """从队列取图 → 识图 → 写结果。"""
        while True:
            item = await queue.get()
            if item is None:  # 结束哨兵
                return
            ref, blob, mime, error = item
            try:
                if error or not blob or not mime:
                    results[ref["token"]] = _failed(ref, error or "下载失败")
                    continue
                # ⚠️ 预算耗尽 ⇒ 剩下的图**直接放弃**（占位符随后被删掉，正文完整）。
                # 宁可这一篇没有图注，也不能让用户多等 —— 剪藏总时限是硬约束。
                timeout = MEDIA_OCR_TIMEOUT
                if deadline is not None:
                    if deadline.expired():
                        results[ref["token"]] = _failed(ref, "抓取预算已耗尽（没时间识图了）")
                        continue
                    timeout = deadline.slice(MEDIA_OCR_TIMEOUT) or MEDIA_OCR_TIMEOUT
                try:
                    async with _global_ocr_semaphore():
                        text = await ocr_image_bytes(
                            blob,
                            mime,
                            prompt=_MEDIA_OCR_PROMPT,
                            timeout=timeout,
                            transport=transport,
                        )
                except Exception as exc:  # noqa: BLE001 —— 一张图认不出来不该炸掉整篇
                    results[ref["token"]] = _failed(ref, f"识图失败: {type(exc).__name__}: {exc}")
                    continue
                text = (text or "").strip()
                if not text:
                    results[ref["token"]] = _failed(ref, "识图结果为空")
                    continue
                results[ref["token"]] = {
                    "url": ref["url"],
                    "token": ref["token"],
                    "caption": text,
                    "ok": True,
                    "error": None,
                }
            except Exception as exc:  # noqa: BLE001 —— 兜底：消费者绝不能死
                results[ref["token"]] = _failed(ref, f"识图流程异常: {type(exc).__name__}: {exc}")

    consumers = [asyncio.create_task(_consumer()) for _ in range(MEDIA_OCR_CONCURRENCY)]
    producers = [asyncio.create_task(_producer(ref)) for ref in attempted]
    try:
        await asyncio.gather(*producers)
    finally:
        # 放结束哨兵，消费者逐个退出。
        for _ in consumers:
            await queue.put(None)
        await asyncio.gather(*consumers, return_exceptions=True)
        # 只有自己建的 client 才关（池里的 client 关了会连累别的 fetcher）。
        if transport is not None:
            await client.aclose()

    for ref in attempted:
        caption = results.get(ref["token"])
        if caption is None:  # 理论上不会发生（每条路径都写了 results）
            results[ref["token"]] = _failed(ref, "识图未完成")
        log.info(
            "media_caption url=%s ok=%s error=%s",
            ref["url"],
            results[ref["token"]]["ok"],
            results[ref["token"]]["error"],
        )
    return _ordered(images, results)


def _ordered(images: list[ImageRef], results: dict[str, MediaCaption]) -> list[MediaCaption]:
    """按输入顺序返回结果（缺的一条补失败记录）。"""
    out: list[MediaCaption] = []
    for ref in images:
        caption = results.get(ref["token"])
        if caption is None:
            caption = _failed(ref, "未处理")
        out.append(caption)
    return out


def _tidy(text: str) -> str:
    """删掉占位符后残留的空行/多余空格。"""
    lines = [line.strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        # 连续空行折叠成一段（正文里的段落分隔是 "\n\n"）
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()


def apply_captions(text: str, captions: list[MediaCaption]) -> str:
    """把正文里的占位符换成图注；失败的（以及没被处理的）**直接删掉**。

    失败不留残迹：占位符删掉后正文就是"少了这一张图"的原文，
    而不是多出一段"（图片识别失败）"这种噪声。
    """
    if not TOKEN_RE.search(text):
        return text
    mapping = {c["token"]: c for c in captions if c.get("token")}

    def _replace(match: re.Match[str]) -> str:
        caption = mapping.get(match.group(0))
        if not caption or not caption.get("ok"):
            return ""
        body = (caption.get("caption") or "").strip()
        if not body:
            return ""
        return f"{CAPTION_PREFIX}{body}"

    return _tidy(TOKEN_RE.sub(_replace, text))


def strip_media_tokens(text: str) -> str:
    """把正文里所有占位符删掉（兜底：绝不让占位符进 LLM）。"""
    if not TOKEN_RE.search(text):
        return text
    return _tidy(TOKEN_RE.sub("", text))


async def caption_media_into(
    result: FetchResult,
    *,
    referer: str | None = None,
    limit: int = MAX_MEDIA_CAPTIONS,
    profile: HeaderProfile = HeaderProfile.DESKTOP,
    transport: httpx.AsyncBaseTransport | None = None,
    deadline: Deadline | None = None,
) -> None:
    """把 `FetchResult` 正文里的配图占位符**就地**换成图注。

    - `raw_metadata["media_refs"]`（解析阶段留下的图清单）会被取走；
    - 成功后 `raw_metadata["media_captions"]` 里有每张图的成功/失败记录；
    - **任何异常都不许让抓取失败**（配图是锦上添花，正文才是硬伤）：
      出错就把占位符删掉，保证正文里不留残迹。

    ⚠️ 落库的记录里，占位符是**可见记法**（``\\x00IMG1\\x00``）而不是带 NUL 的
    真占位符 —— JSONB 不接受 ``\\u0000``（见 `json_safe_token`）。
    """
    stored = result.raw_metadata.pop("media_refs", None) or []
    refs = refs_from_storage(stored)
    if not refs:
        return
    try:
        captions = await caption_media(
            refs,
            referer=referer or result.raw_metadata.get("final_url") or result.url,
            limit=limit,
            profile=profile,
            transport=transport,
            deadline=deadline,
        )
        result.content_text = apply_captions(result.content_text, captions)
        result.raw_metadata["media_captions"] = [
            {**caption, "token": json_safe_token(caption["token"])} for caption in captions
        ]
    except Exception as exc:  # noqa: BLE001 —— 整段流程不许让抓取失败
        log.warning("media_caption_failed err=%s: %s", type(exc).__name__, exc)
        result.content_text = strip_media_tokens(result.content_text)
        result.raw_metadata["media_captions"] = []


__all__ = [
    "CAPTION_PREFIX",
    "MAX_MEDIA_BYTES",
    "MAX_MEDIA_CAPTIONS",
    "MEDIA_DOWNLOAD_CONCURRENCY",
    "MEDIA_DOWNLOAD_TIMEOUT",
    "MEDIA_OCR_CONCURRENCY",
    "MEDIA_OCR_GLOBAL_CONCURRENCY",
    "MEDIA_OCR_QUEUE_SIZE",
    "MEDIA_OCR_TIMEOUT",
    "ImageRef",
    "MediaCaption",
    "MediaImageCollector",
    "apply_captions",
    "caption_media",
    "caption_media_into",
    "json_safe_token",
    "make_token",
    "pick_image_src",
    "refs_from_storage",
    "strip_media_tokens",
    "token_index",
]
