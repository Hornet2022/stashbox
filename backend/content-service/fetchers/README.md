# fetchers/ —— 抓取核心抽象层（v1 §11.2 CP2.1）

content-service 的**收集层**抽象。本期只落接口 + 占位实现，真抓取在 CP2.2-CP2.4。

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
| `WechatFetcher` | `wechat_mp` | `mp.weixin.qq.com` | CP2.1 占位（fetch 抛 UNSUPPORTED） |
| `DouyinFetcher` | `douyin` | `douyin.com` / `iesdouyin.com`（含 `v.douyin.com` 短链） | CP2.1 占位 |
| `GenericURLFetcher` | `generic_url` | 恒 `True`（catch-all） | CP2.1 占位 |
| `MockFetcher` | — | — | **本期不写**，留给 CP2.7 集成测（10 个真实 URL 里垫刀用） |

路线：

- **CP2.2 公众号**（`wechat.py`）：走 `mp.weixin.qq.com/s/xxx` 正文页，取标题 / 作者 / 正文
  HTML+纯文本 / 图片直链；防爬时需要处理 `环境异常` 验证页 → `AUTH`。
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
