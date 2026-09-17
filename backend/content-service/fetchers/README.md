# fetchers/ —— 抓取核心抽象层（v1 §11.2 CP2.1）

content-service 的**收集层**抽象。本期只落接口 + 占位实现，真抓取在 CP2.2-CP2.4。

模块分工（CP2.2 起解析部分共用）：

```
base.py        契约（CP2.1 定死：Fetcher / FetchResult / FetcherError）
parser.py      共享 HTML 解析器（CP2.2 抽出来的 5 个 HTMLParser + 3 个工具函数）
wechat.py      WechatFetcher  —— CP2.2 实现，复用 parser.py
douyin.py      DouyinFetcher  —— CP2.1 占位
generic_url.py GenericURLFetcher —— CP2.4 实现，复用 parser.py
```

## 1. 接口契约（`base.py`）

```
Fetcher(ABC)
├── name -> str                      # 属性，"wechat_mp" / "douyin" / "generic_url"
├── supports(url) -> bool            # URL pattern 匹配（同步，纯字符串判断，不发请求）
└── async fetch(url, *, timeout=30.0) -> FetchResult
```

- `fetch()` 是 async：CP2.2+ 要发 HTTP，不能阻塞 event loop。
- `timeout` 默认 30s，CP2.7 集成测可调。
- **失败一律抛 `FetcherError`**（不返回 None / 不返回空结果），调用方只需 try/except 一种异常。

```python
@dataclass
class FetchResult:
    url: str                    # 原始 URL
    title: str
    content_html: str           # 清洗后的 HTML（CP2 不存原始 HTML）
    content_text: str           # 纯文本，喂给 Step 1 多模态理解
    author: str | None = None
    publish_time: datetime | None = None   # 带时区
    media_urls: list[str] = []  # 图片/视频直链
    source: str = "unknown"     # 与 Fetcher.name 对齐
    raw_metadata: dict = {}     # 原始元数据，CP2 调试用
```

错误码 `FetcherErrorCode`：`network` / `parse` / `auth` / `rate_limit` / `not_found` /
`unsupported` / `internal`，均带 `fetcher.` 前缀。**这是 fetcher 私有错误码，不并入
v1 §3.x 的 `biz_code` 体系** —— 对外暴露时由上层（CP2.5）映射成统一错误响应。

异常字符串格式固定为 `[source] code: message`，例：`[wechat_mp] fetcher.network: timeout`。

## 2. 4 个 Fetcher 当前状态

| Fetcher | `name` | `supports()` 匹配 | 状态 |
|---|---|---|---|
| `WechatFetcher` | `wechat_mp` | `mp.weixin.qq.com` | **CP2.2 实现**（httpx + `#js_content` 专属选择器） |
| `DouyinFetcher` | `douyin` | `douyin.com` / `iesdouyin.com`（含 `v.douyin.com` 短链） | CP2.1 占位 |
| `GenericURLFetcher` | `generic_url` | 恒 `True`（catch-all） | **CP2.4 实现**（httpx + stdlib HTMLParser） |
| `MockFetcher` | — | — | **本期不写**，留给 CP2.7 集成测（10 个真实 URL 里垫刀用） |

路线：

- **CP2.2 公众号**（`wechat.py`）：走 `mp.weixin.qq.com/s/xxx` 正文页，取标题 / 作者 / 正文
  HTML+纯文本 / 图片直链；`环境异常` 验证页 → `AUTH`。**已实现**，细节见 §6。
- **CP2.3 抖音**（`douyin.py`）：先解 `v.douyin.com` 短链 302，再从页面内嵌数据取视频/图集；
  正文常常只在 JS 渲染后的数据里，可能要解析内嵌 JSON → 失败走 `PARSE`。
- **CP2.4 通用 URL**（`generic_url.py`）：任意网页的正文抽取（readability 那类思路）。
- 三个实现都**不改 `base.py`** —— 契约本期定死，改契约要单独提。

## 3. 工厂 `get_fetcher(url)` 的匹配优先级

`__init__.py` 里 `_ALL_FETCHERS` 的**列表顺序即优先级**，从上往下第一个 `supports()` 为 True 的胜出：

```
WechatFetcher  →  DouyinFetcher  →  GenericURLFetcher
  (专属域名)        (专属域名)         (catch-all，永远 True)
```

```python
get_fetcher("https://mp.weixin.qq.com/s/abc")  # -> WechatFetcher
get_fetcher("https://v.douyin.com/i12345")     # -> DouyinFetcher
get_fetcher("https://example.com/article")     # -> GenericURLFetcher
get_fetcher("not-a-url")                       # -> GenericURLFetcher（兜底，不是 None）
```

新增 fetcher 时：**专属域名的一定要加在 `GenericURLFetcher()` 之前**，否则会被兜底抢走。
当前依赖 `GenericURLFetcher` 兜底，所以 `get_fetcher()` 在实践中不会返回 `None`
（签名保留 `| None` 是为了将来把兜底关掉时不用改调用方）。

## 4. CP2.5 服务号 Handler 怎么用

```python
fetcher = get_fetcher(url)
if fetcher is None:
    ...  # 当前不会走到（generic_url 兜底）
result = await fetcher.fetch(url)   # -> FetchResult，失败抛 FetcherError
```

Handler 侧建议的写法：

```python
from fetchers import FetcherError, get_fetcher   # content-service 目录带连字符，按 sys.path 导入

try:
    fetcher = get_fetcher(url)
    result = await fetcher.fetch(url, timeout=30.0)
except FetcherError as e:
    logger.warning("fetch failed: %s", e)   # [source] code: message
    ...                                     # 映射成 v1 §3.x 错误码返回给服务号
```

CP2.5 之前**不要**在主流程里调 `get_fetcher()` —— 本期只是抽象层，没接进 `main.py`。

## 5. GenericURLFetcher 当前能力（CP2.4）

`generic_url.py`：`httpx.AsyncClient` 抓 HTML + 5 个 stdlib `HTMLParser` 解析，**不装
beautifulsoup4 / readability-lxml / lxml**（依赖只有已有的 httpx 0.28.1）。

5 个解析器 CP2.2 起住在 **`parser.py`**（公众号抓取器共用），`generic_url.py` 用
`from .parser import ...` 复用，行为不变（表里的 `_XxxExtractor` 旧名仍从 `generic_url` 导出）。

```
fetch(url)
├── scheme 不是 http/https        → FetcherError(UNSUPPORTED)
├── 超时 / 传输层异常              → FetcherError(NETWORK)
├── 404 / 410                     → FetcherError(NOT_FOUND)
├── 其它 >= 400                    → FetcherError(NETWORK, "http {status}")
├── content-type 不是 html/xhtml  → FetcherError(PARSE)
├── 抽不出任何正文                 → FetcherError(PARSE, "no article content extracted")
└── FetchResult（source="generic_url"）
```

| 解析器 | 抽什么 |
|---|---|
| `_TitleExtractor` | `og:title` 优先，退回 `<title>` |
| `_AuthorExtractor` | `meta[name=author]` → `article:author` → `twitter:creator` |
| `_TimeExtractor` | `article:published_time` → `pubdate` → `<time datetime>`（ISO 8601 / RFC 822，无时区按 UTC） |
| `_MediaExtractor` | `og:image` / `og:video` + `<img src>`，`urljoin(base_url, src)` 转绝对（base 用**redirect 之后**的最终 URL） |
| `_ContentExtractor` | readability 简化：密度最高的正文段落 |

**密度启发式**（阈值都是模块常量，可直接调）：

- 候选标签 `<p>` / `<div>` / `<article>` / `<section>`；**只保留叶子段**——包着别的候选段的
  容器（`<article>`、外层 `<div>`）不参与竞争，否则整页会作为一段胜出。
- 密度 = `纯文本长度 / 原始 HTML 长度`；丢弃 `< 3` 字符的段，丢弃密度 `< 0.25` 的段
  （导航/侧栏那种塞满 `<a>` 的段密度通常在 0.1 上下，正文在 0.9 上下）。
- 取密度最高的 **10 段**，再按文档顺序输出；`content_text` 用空行拼接，`content_html`
  是这些段落的原始 HTML 片段拼接。
- 防爆：单段原始 HTML > 100KB 丢弃、单段文本截到 20KB、`content_text` 截到 50KB、
  `content_html` 按整段累加到 200KB（不切断标签）、解析前 HTML 截到 2MB。
- 跳过 `<script>` / `<style>` / `<noscript>` / `<template>` / `<svg>` / `<iframe>` 的内容；
  `<nav>` / `<header>` / `<footer>` / `<aside>` / `<form>` 里的候选段直接判为样板丢弃。

**已知限制**：

- JS 渲染页（React/Vue SPA、正文由接口异步注入）拿不到正文 —— 拿到的只有空壳 HTML，
  会抛 `PARSE: no article content extracted`。
- 很简单的 HTML（正文就一句话、或者整页只有一个 `<div>`）会被密度阈值误判为导航而丢空。
- 正文在 `<span>` / `<li>` / `<td>` 里的页面抽不到（候选标签只认 4 个块级标签）。
- 编码只认 HTTP header 的 charset，`<meta charset>` 声明的 GBK 类页面可能乱码
  （`response.text` 的 httpx 默认行为）。
- 懒加载图片的 `data-src` 不收，只收 `<img src>`。

**CP2.7 集成测怎么用**：10 个真实 URL 里，公众号/抖音走各自 fetcher，剩下的（知乎、少数派、
个人博客、新闻站之类）都兜底到 `GenericURLFetcher`：

```python
from fetchers import get_fetcher

fetcher = get_fetcher(url)              # 非公众号/抖音 → GenericURLFetcher
try:
    result = await fetcher.fetch(url, timeout=30.0)
except FetcherError as e:               # [generic_url] code: message
    ...
```

建议 5 个 URL 里至少含 1 个 JS 渲染站（验证 PARSE 兜底）+ 1 个带分页/侧栏的长文
（验证密度启发式）。抽完打印 `result.raw_metadata`：`paragraph_count` 是最终选中段数、
`candidate_count` 是通过阈值的候选段数，两者差太多说明阈值要调。

## 6. WechatFetcher 当前能力（CP2.2）

`wechat.py`：`httpx.AsyncClient` + iPhone UA + `Referer: https://mp.weixin.qq.com/`，
单实例抓取（本期**没有**代理池 / cookie 池）。解析复用 `parser.py` 的
Title / Author / Time / Media 四个解析器，正文走公众号专属容器，不用密度启发式。

```
fetch(url)
├── 不是 mp.weixin.qq.com              → FetcherError(UNSUPPORTED)（supports() 说了算，不发请求）
├── 超时 / 传输层异常                   → FetcherError(NETWORK)
├── 404 / 410                          → FetcherError(NOT_FOUND)
├── 其它 >= 400                         → FetcherError(NETWORK, "http {status}")
├── 命中反爬/失效提示页                  → FetcherError(AUTH / NOT_FOUND)，见下表
├── 拿不到 #js_content 或正文为空        → FetcherError(PARSE)
└── FetchResult（source="wechat_mp"）
```

| 页面特征 | 错误码 | 含义 |
|---|---|---|
| `环境异常` | `AUTH` | 微信风控判定非真人环境（最常见的反爬页） |
| `请在微信中打开` | `AUTH` | 必须在微信内置浏览器 |
| `该公众号已迁移` | `NOT_FOUND` | 账号迁移，原文不再可达 |
| `此内容因违规无法查看` | `AUTH` | 内容被处置 |

公众号专属选择器（和通用页不同的地方，都在 `fetchers/wechat.py` 里）：

| 字段 | 取法 |
|---|---|
| 正文 HTML | `<div id="js_content">` 正则抽内部 html（后面紧跟 `<script>` 时非贪婪截止；没有则兜底吃到文末） |
| 正文纯文本 | `_strip_tags()`（自制 HTMLParser，跳过 script/style）+ `_norm()` 折叠空白 |
| 标题 | `<title>` / `og:title`，再剥掉 ` - 公众号名` 后缀（公众号名来自 `<a id="js_name">`） |
| 作者 | `<meta name="author">` 优先，退回公众号名 |
| 发布时间 | `article:published_time` 优先，退回 `<em id="publish_time">`（`2026-09-17 08:30` 这种，无时区按 UTC） |
| 图片 | `og:image` + 正文里懒加载的 `<img data-src>`（`data-src` 才是真图，`src` 常是占位） |

**已知限制**：

- 只能用 `data-src` / `og:image` 拿静态直链，图文里的视频（`v.qq.com` iframe）拿不到。
- 反爬加严（出现验证码 / 需要登录 cookie）时仍会落到 `AUTH` —— 本期只识别不破解。
  **如果未来要加代理 / cookie 池，扩展点是 `WechatFetcher._download()`，不是 `base.py` 契约。**
- 正文里有嵌套 `<div>` 且外层不是 `js_content` 时，非贪婪截止依赖 `</div>\s*<script`；
  兜底分支会把文末的推荐位 / 页脚一起吃进来（真实公众号页面 `js_content` 后紧跟脚本，罕见）。

**CP2.7 集成测**：公众号 URL 现在走 `WechatFetcher`，不再抛 2001（之前是 UNSUPPORTED → 2001）。
真机抓取要用 iPhone UA，`content-service/tests/fetchers/fixtures/wechat_article.html` 是测试用的样本页。
