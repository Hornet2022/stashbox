"""正文配图识图（2026-10-25）。

## 识图现在分**两段**（2026-10-25 改：挪出剪藏请求）

    ① 抓取（fetch）  → 正文里留**占位符**，图清单进 raw_metadata["media_refs"]
    ② 补图注（蒸馏前）→ 占位符换成图注文字，清单换成 raw_metadata["media_captions"]

⚠️ 用例也按这两段写：**fetch 之后不该再有任何"图表："**——
那正是这次改造要保证的（识图不许再占剪藏请求的时间）。

## 为什么要有这些用例

配图识图这条链路横跨**三段**，每一段都能安静地坏掉：

1. **解析段** —— 正文里的 `<img>` 要留下占位符，而 `og:image`（封面）
   **不能**留下（封面不是内容）；
2. **下载 + 识图段** —— 图要真能下载、能真能认出字；
3. **注入段** —— 认出来的文字要回到**原来的位置**；
   认不出来（下载失败 / 识图为空 / 超上限）时占位符必须**被删干净**。

第 3 条里"删干净"是最容易漏的：占位符是 `\\x00IMG1\\x00` 这种 NUL 哨兵，
留在正文里不会被任何"看起来对不对"的检查发现，但它会**原样进 LLM 的 prompt**
（下游 prompt 明确禁止这类奇怪标记）。所以每条失败路径都要断言
"正文里一个 `\\x00` 都不剩"。

## 全部不打真网络

图片下载和识图请求都走 `httpx.MockTransport` 注入假响应；
内网地址的拦截按 `common/ssrf.py` 的规则**断言**，不发真实请求。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
CONTENT_DIR = BACKEND_DIR / "content-service"
if str(CONTENT_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_DIR))

# content-service 目录名带连字符，按文件路径加载它的 fetchers 包
_spec = importlib.util.spec_from_file_location(
    "cs_media_fetchers",
    CONTENT_DIR / "fetchers" / "__init__.py",
    submodule_search_locations=[str(CONTENT_DIR / "fetchers")],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cs_media_fetchers"] = _mod
_spec.loader.exec_module(_mod)

media_ocr = importlib.import_module("cs_media_fetchers.media_ocr")
net = importlib.import_module("cs_media_fetchers.net")
ocr_mod = importlib.import_module("cs_media_fetchers.ocr")
generic_mod = importlib.import_module("cs_media_fetchers.generic_url")
wechat_mod = importlib.import_module("cs_media_fetchers.wechat")

from stashbox.backend.common import media_placeholders as placeholders  # noqa: E402
from stashbox.backend.common.ssrf import (  # noqa: E402
    SsrfBlocked,
    assert_public_url,
)

#: 一张"合法图片"的最小字节（PNG 魔数 + 一点载荷）。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload" * 8

#: 识图返回的默认文字。
DEFAULT_CAPTION = "2024 年营收增速 30%，Q4 贡献最大。"

ARTICLE_URL = "https://mp.weixin.qq.com/s/XyZ123AbC789"
GENERIC_URL = "https://example.com/2026/10/25/chart-article"


class _NoThrottle:
    """测试里不等节流（默认同 host 间隔 0.3s，几十个用例就是纯等待）。"""

    async def wait(self, host: str) -> float:  # noqa: ARG002
        return 0.0


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(media_ocr, "HOST_THROTTLE", _NoThrottle())


def _config_stub(
    base_url: str = "https://provider.example/v1", api_key: str = "k", model: str = "m1"
):
    """假 provider 配置（不去碰 DB）。"""

    async def _stub():
        return base_url, api_key, model

    return _stub


@pytest.fixture
def fake_provider(monkeypatch):
    """把识图通道换成本地假配置（否则会读 DB / 真 provider）。"""
    monkeypatch.setattr(ocr_mod, "_resolve_config", _config_stub())


def _transport(
    *,
    article_html: str | None = None,
    article_url: str = "",
    caption: str | None = DEFAULT_CAPTION,
    image_status: int = 200,
    image_body: bytes = PNG_BYTES,
    seen: dict[str, httpx.Headers] | None = None,
) -> httpx.MockTransport:
    """一个 transport 同时扮演**文章页**、**图床**和**识图 provider**。

    按 URL/路径分流：
    - `/chat/completions` → provider；
    - 以 `article_url` 开头 → 文章页；
    - 其余 → 图片。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen[str(request.url)] = request.headers
        if request.url.path.endswith("/chat/completions"):
            content = caption if caption is not None else ""
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        if article_html is not None and str(request.url).startswith(article_url):
            return httpx.Response(
                200, text=article_html, headers={"content-type": "text/html; charset=utf-8"}
            )
        if image_status != 200:
            return httpx.Response(image_status, text="boom")
        return httpx.Response(200, content=image_body)

    return httpx.MockTransport(handler)


def _refs(count: int = 1, host: str = "https://cdn.example.com") -> list[dict]:
    return [{"url": f"{host}/fig{i}.png", "token": f"\x00IMG{i}\x00"} for i in range(1, count + 1)]


def _stored(result) -> dict:
    """FetchResult → **落库形态**的 raw_content（占位符转成可见记法）。

    为什么手写而不复用 content-service 的 `_to_raw_content`：
    那个函数住在 `content-service/main.py` 里，import 它会把整个服务的依赖
    （DB / Redis / 全部 router）拖进这个**纯单测**。

    契约本身在 `common/media_placeholders.py`（两个服务共用一份），
    这里复用的就是它的 `to_storage_text` ⇒ 不会与实现漂移。
    """
    return {
        "url": result.url,
        "source": result.source,
        "content_text": placeholders.to_storage_text(result.content_text),
        "raw_metadata": json.loads(json.dumps(result.raw_metadata, ensure_ascii=False)),
    }


# ---------------------------------------------------------------------------
# 1 + 3. 识图成功 ⇒ 占位符被替换成图注文本（且在原位）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_图被下载并识图_占位符被替换成图注文本(fake_provider):
    refs = _refs(1)
    captions = await media_ocr.caption_media(refs, transport=_transport())

    assert len(captions) == 1
    assert captions[0]["ok"] is True
    assert captions[0]["error"] is None
    assert captions[0]["caption"] == DEFAULT_CAPTION
    assert captions[0]["token"] == "\x00IMG1\x00"

    text = "第一段讲背景。\n\n\x00IMG1\x00\n\n第二段讲结论。"
    out = media_ocr.apply_captions(text, captions)

    assert "\x00" not in out, "正文里不能残留占位符"
    assert "图表：2024 年营收增速 30%，Q4 贡献最大。" in out
    # 位置：图注在第一段之后、第二段之前（**原位**，不是堆在文末）
    assert out.index("第一段") < out.index("图表") < out.index("第二段")


@pytest.mark.asyncio
async def test_普通网页_图注回到图在正文里的原位(fake_provider):
    """通用网页：两张图分别插在第 1 段后、第 2 段后。

    ⚠️ 两段式（2026-10-25）：① 抓取只留占位符；② 补图注后图注回原位。
    """
    html = (
        "<!DOCTYPE html><html><head>"
        '<meta property="og:image" content="https://cdn.example.com/og-cover.jpg">'
        "<title>带图表的文章</title></head><body><article>"
        "<p>" + "这是第一段正文，密度启发式会选中它。" * 8 + "</p>"
        '<img src="https://cdn.example.com/fig1.png" alt="图一">'
        "<p>" + "这是第二段正文，图一应该在它前面。" * 8 + "</p>"
        '<img src="/static/fig2.png" alt="图二">'
        "<p>" + "这是第三段正文，图二应该在它前面。" * 8 + "</p>"
        "</article></body></html>"
    )
    transport = _transport(article_html=html, article_url=GENERIC_URL)
    fetcher = generic_mod.GenericURLFetcher(transport=transport)
    result = await fetcher.fetch(GENERIC_URL)

    # ── ① 抓取阶段：只留占位符，**不识图** ──────────────────────────
    assert "图表：" not in result.content_text, "识图不在抓取阶段做（2026-10-25 改）"
    assert result.content_text.count("\x00IMG") == 2
    assert len(result.raw_metadata["media_refs"]) == 2

    # ── ② 补图注（蒸馏前那一步）────────────────────────────────────
    raw, stats = await media_ocr.caption_stored_content(_stored(result), transport=transport)

    assert stats["images"] == 2
    assert stats["captions"] == 2
    body = raw["content_text"]
    assert "\x00" not in body
    assert body.count("图表：") == 2
    assert body.index("第一段") < body.index("图表") < body.index("第二段")
    assert body.index("第二段") < body.index("图表", body.index("第二段")) < body.index("第三段")


# ---------------------------------------------------------------------------
# 2. 下载失败 ⇒ 占位符被删除，正文里不残留任何 \x00
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_下载失败_占位符被删干净(fake_provider):
    refs = _refs(1)
    captions = await media_ocr.caption_media(refs, transport=_transport(image_status=404))

    assert captions[0]["ok"] is False
    assert captions[0]["error"], "失败必须记原因（否则查不到为什么没有图表信息）"
    assert captions[0]["caption"] == ""

    out = media_ocr.apply_captions("第一段。\n\n\x00IMG1\x00\n\n第二段。", captions)
    assert "\x00" not in out
    assert "第一段" in out and "第二段" in out
    assert "图表" not in out


# ---------------------------------------------------------------------------
# 3. 识图返回空 ⇒ 同上
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_识图返回空_占位符被删干净(fake_provider):
    refs = _refs(1)
    captions = await media_ocr.caption_media(refs, transport=_transport(caption=""))

    assert captions[0]["ok"] is False
    assert captions[0]["error"]

    out = media_ocr.apply_captions("第一段。\n\n\x00IMG1\x00\n\n第二段。", captions)
    assert "\x00" not in out
    assert "第一段" in out and "第二段" in out


# ---------------------------------------------------------------------------
# 4. 限量生效：超过上限的图不识，占位符被删除
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_限量生效_超过上限的占位符被删除(fake_provider):
    """上限**仍然生效**（显式传 limit=3 来测，不依赖默认值）。

    超出的图：不下载、不识图，图注记失败，占位符**删干净**。
    """
    seen: dict[str, httpx.Headers] = {}
    refs = _refs(8)
    captions = await media_ocr.caption_media(refs, transport=_transport(seen=seen), limit=3)

    assert len(captions) == 8
    ok = [c for c in captions if c["ok"]]
    assert len(ok) == 3
    # 只下载了 3 张图（多下的就是白烧流量 + 白烧图床的耐心）
    image_requests = [u for u in seen if not u.endswith("/chat/completions")]
    assert len(image_requests) == 3
    # 落选的那几张要说清为什么（"为什么没有图表信息"查得到原因）
    skipped = [c for c in captions if not c["ok"]]
    assert len(skipped) == 5
    assert all("超出单篇识图上限" in (c["error"] or "") for c in skipped)

    text = "\n\n".join(f"\x00IMG{i}\x00" for i in range(1, 9))
    out = media_ocr.apply_captions(text, captions)
    assert "\x00" not in out
    assert out.count("图表：") == 3


def test_默认上限不许退回旧值():
    """上限是 12 —— 改小**不报错**，只是让 67% 的文章白白丢图 ⇒ 必须挡住。

    依据（2026-10-25 生产实测）：OCR 并发 3 时 30s 预算内能识完 12 张；
    生产 33 篇里 22 篇图数 > 5，旧上限 5 让三分之二的文章丢图。
    """
    assert media_ocr.MAX_MEDIA_CAPTIONS == 12


# ---------------------------------------------------------------------------
# 4b. 并发：并发度有上限、结果顺序不变、每张图的图注挂在自己身上
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_识图并发不超过单篇上限(monkeypatch, fake_provider):
    """并发是**有闸门**的：不能因为改成并发就把 provider 打穿。

    同时断言「并发真的生效了」—— 否则这个改造等于没做。
    """
    state = {"now": 0, "peak": 0}

    async def _fake_ocr(blob, mime, *, prompt=None, timeout=None, transport=None):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return DEFAULT_CAPTION

    monkeypatch.setattr(media_ocr, "ocr_image_bytes", _fake_ocr)

    refs = _refs(10)
    captions = await media_ocr.caption_media(refs, transport=_transport())

    assert all(c["ok"] for c in captions)
    assert state["peak"] <= media_ocr.MEDIA_OCR_CONCURRENCY, f"并发峰值 {state['peak']} 超了闸门"
    assert state["peak"] > 1, "并发没生效（串行的话这次改造就没意义了）"


@pytest.mark.asyncio
async def test_并发下每张图的图注挂在自己身上(monkeypatch, fake_provider):
    """并发最容易出的错：图 A 的图注挂到图 B 的占位符上（正文直接串位）。

    做法：每张图给**不同的字节**，假识图从字节里认出自己的编号；
    并让靠后的图先返回（逼出「谁先完成谁先写」的那类 bug）。
    """
    bodies = {
        f"https://cdn.example.com/fig{i}.png": PNG_BYTES + f"FIG{i}".encode() for i in range(1, 7)
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": DEFAULT_CAPTION}}]}
            )
        body = bodies.get(str(request.url))
        return httpx.Response(200, content=body or PNG_BYTES)

    async def _fake_ocr(blob, mime, *, prompt=None, timeout=None, transport=None):
        raw = blob.decode("latin-1")
        digits = "".join(ch for ch in raw.split("FIG", 1)[-1] if ch.isdigit())
        n = int(digits or 0)
        await asyncio.sleep(0.01 * (6 - n))  # 编号大的先返回
        return f"图 {n} 的内容"

    monkeypatch.setattr(media_ocr, "ocr_image_bytes", _fake_ocr)

    refs = _refs(6)
    captions = await media_ocr.caption_media(refs, transport=httpx.MockTransport(handler))

    for i, cap in enumerate(captions, 1):
        assert cap["ok"], cap
        assert cap["caption"] == f"图 {i} 的内容", f"第 {i} 张图的图注串位了：{cap}"


# ---------------------------------------------------------------------------
# 5. 封面图不进正文占位符（og:image 不留占位符）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_封面图_og_image_不进正文占位符(fake_provider):
    """`og:image` 是 `<meta>` 不是 `<img>` ⇒ 天然不该进正文。"""
    html = (
        "<!DOCTYPE html><html><head>"
        '<meta property="og:image" content="https://cdn.example.com/og-cover.jpg">'
        "<title>带封面的文章</title></head><body><article>"
        "<p>" + "这是正文，密度启发式会选中它。" * 20 + "</p>"
        '<img src="https://cdn.example.com/fig1.png" alt="正文图">'
        "</article></body></html>"
    )
    fetcher = generic_mod.GenericURLFetcher(
        transport=_transport(article_html=html, article_url=GENERIC_URL)
    )
    result = await fetcher.fetch(GENERIC_URL)

    # 封面 URL 在 media_urls 里（那是封面资源），但正文里没有它的位置
    assert "https://cdn.example.com/og-cover.jpg" in result.media_urls
    assert "og-cover" not in result.content_text
    # 只登记了正文里那一张图 —— 封面压根没进配图清单
    refs = result.raw_metadata["media_refs"]
    assert len(refs) == 1
    assert refs[0]["url"] == "https://cdn.example.com/fig1.png"


@pytest.mark.asyncio
async def test_公众号_封面图_不进正文占位符(fake_provider):
    """公众号的 og:image（封面）同样不进正文；正文里的 data-src 图要进。"""
    html = (
        "<html><head>"
        '<meta property="og:image" content="https://mmbiz.qpic.cn/mmbiz_jpg/cover.jpg">'
        "<title>公众号文章 - 某公众号</title></head><body>"
        '<div id="js_content">'
        "<p>" + "公众号正文段落，讲的是数据。" * 20 + "</p>"
        '<img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/body1.jpg?wx_fmt=jpeg">'
        "<p>" + "第二段公众号正文。" * 20 + "</p>"
        "</div></body></html>"
    )
    transport = _transport(article_html=html, article_url=ARTICLE_URL)
    fetcher = wechat_mod.WechatFetcher(transport=transport)
    result = await fetcher.fetch(ARTICLE_URL)

    assert "https://mmbiz.qpic.cn/mmbiz_jpg/cover.jpg" in result.media_urls
    assert "cover.jpg" not in result.content_text
    # 只登记了正文里那一张（封面没进配图清单）
    refs = result.raw_metadata["media_refs"]
    assert len(refs) == 1
    assert refs[0]["url"] == "https://mmbiz.qpic.cn/mmbiz_jpg/body1.jpg?wx_fmt=jpeg"

    raw, _ = await media_ocr.caption_stored_content(_stored(result), transport=transport)
    assert raw["content_text"].count("图表：") == 1


# ---------------------------------------------------------------------------
# 6. SSRF 防护在位（按 common/ssrf.py 的规则断言，不发真实请求）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ssrf_防护在位_内网地址被拦():
    """内网/回环地址按 `common/ssrf.py` 的规则判为阻断。"""
    for url in (
        "http://127.0.0.1:6379/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/x",
    ):
        with pytest.raises(SsrfBlocked):
            assert_public_url(url)

    # 生产路径建 client 时必须挂上 SSRF request hook
    kwargs = net.build_client_kwargs(net.HeaderProfile.DESKTOP, guard_ssrf=True)
    assert "event_hooks" in kwargs
    assert kwargs["event_hooks"]["request"], "必须挂 request hook（发出前拦）"

    # hook 本身也拦得住内网目标（不发真实请求）
    request = httpx.Request("GET", "http://127.0.0.1:6379/")
    with pytest.raises(net.SsrfBlockedError):
        await net._ssrf_request_hook(request)


@pytest.mark.asyncio
async def test_生产路径_图片下载客户端带_ssrf_防护(monkeypatch, fake_provider):
    """不注入 MockTransport 时（生产路径），下载 client 必须带 guard_ssrf=True。"""
    captured: dict = {}

    class _Stop(Exception):
        pass

    def fake_acquire(profile, **kwargs):  # noqa: ANN001, ANN202
        captured["profile"] = profile
        captured.update(kwargs)
        raise _Stop

    monkeypatch.setattr(media_ocr, "acquire_client", fake_acquire)
    with pytest.raises(_Stop):
        await media_ocr.caption_media(_refs(1))

    # guard_ssrf=True 时 build_client_kwargs 才会挂上 request hook
    assert captured["event_hooks"]["request"], "生产路径必须挂 SSRF request hook"


# ---------------------------------------------------------------------------
# 7. raw_metadata["media_captions"] 里有可查的记录（成功/失败都有）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_raw_metadata_里有成功和失败的图注记录(fake_provider):
    """一篇正文里两张图：一张认得出，一张下载失败 —— 两条记录都要在。"""
    html = (
        "<!DOCTYPE html><html><head><title>图表文章</title></head><body><article>"
        "<p>" + "这是正文，密度启发式会选中它。" * 20 + "</p>"
        '<img src="https://cdn.example.com/ok.png" alt="好图">'
        '<img src="https://cdn.example.com/bad.png" alt="坏图">'
        "</article></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": DEFAULT_CAPTION}}]}
            )
        if request.url.path.endswith("/bad.png"):
            return httpx.Response(404, text="nope")
        if str(request.url).startswith(GENERIC_URL):
            return httpx.Response(
                200, text=html, headers={"content-type": "text/html; charset=utf-8"}
            )
        return httpx.Response(200, content=PNG_BYTES)

    fetcher = generic_mod.GenericURLFetcher(transport=httpx.MockTransport(handler))
    result = await fetcher.fetch(GENERIC_URL)

    raw, stats = await media_ocr.caption_stored_content(
        _stored(result), transport=httpx.MockTransport(handler)
    )
    records = raw["raw_metadata"]["media_captions"]
    assert len(records) == 2
    assert stats["captions"] == 1, "只有认出来的那张算数"
    ok = [r for r in records if r["ok"]]
    bad = [r for r in records if not r["ok"]]
    assert len(ok) == 1 and len(bad) == 1
    assert ok[0]["caption"] == DEFAULT_CAPTION
    assert bad[0]["error"], "失败的那张必须能查到原因"
    # 内部中间态不能漏到落库的 raw_metadata 里
    assert "media_refs" not in raw["raw_metadata"]


# ---------------------------------------------------------------------------
# 8. 微信图床的 Referer 头被带上（防盗链）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_微信图床_请求带_referer(fake_provider):
    seen: dict[str, httpx.Headers] = {}
    img = "https://mmbiz.qpic.cn/mmbiz_jpg/body-scene01.jpg?wx_fmt=jpeg"
    refs = [{"url": img, "token": "\x00IMG1\x00"}]

    await media_ocr.caption_media(
        refs,
        referer="https://mp.weixin.qq.com/s/XyZ123AbC789",
        transport=_transport(seen=seen),
    )

    assert img in seen, "图必须真被下载过"
    assert seen[img]["referer"] == "https://mp.weixin.qq.com/s/XyZ123AbC789"


@pytest.mark.asyncio
async def test_公众号抓取_图片请求带原文_referer(fake_provider):
    """走完整 fetcher 链路时，图床请求的 Referer 是**原文 URL**（防盗链）。"""
    seen: dict[str, httpx.Headers] = {}
    html = (
        "<html><head><title>公众号文章 - 某公众号</title></head><body>"
        '<div id="js_content">'
        "<p>" + "公众号正文段落，讲的是数据。" * 20 + "</p>"
        '<img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/body1.jpg?wx_fmt=jpeg">'
        "</div></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = request.headers
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": DEFAULT_CAPTION}}]}
            )
        if "mp.weixin.qq.com" in request.url.host:
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        return httpx.Response(200, content=PNG_BYTES)

    fetcher = wechat_mod.WechatFetcher(transport=httpx.MockTransport(handler))
    result = await fetcher.fetch(ARTICLE_URL)

    raw, _ = await media_ocr.caption_stored_content(
        _stored(result), transport=httpx.MockTransport(handler)
    )
    img_headers = seen["https://mmbiz.qpic.cn/mmbiz_jpg/body1.jpg?wx_fmt=jpeg"]
    assert img_headers["referer"] == ARTICLE_URL
    assert raw["content_text"].count("图表：") == 1


# ---------------------------------------------------------------------------
# 9. 失败不阻断：整段流程炸了也不能让抓取失败，更不能留下占位符
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_识图流程整体炸掉_抓取不失败且不留占位符(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("provider 炸了")

    monkeypatch.setattr(media_ocr, "ocr_config", _boom)

    html = (
        "<!DOCTYPE html><html><head><title>图表文章</title></head><body><article>"
        "<p>" + "这是正文，密度启发式会选中它。" * 20 + "</p>"
        '<img src="https://cdn.example.com/fig1.png" alt="图">'
        "</article></body></html>"
    )
    transport = _transport(article_html=html, article_url=GENERIC_URL)
    fetcher = generic_mod.GenericURLFetcher(transport=transport)
    result = await fetcher.fetch(GENERIC_URL)

    # 抓取阶段不受影响（识图本来就不在这里做）
    assert "这是正文" in result.content_text

    # 补图注阶段炸了 ⇒ 占位符必须被清干净，正文本身必须完好
    raw, stats = await media_ocr.caption_stored_content(_stored(result), transport=transport)
    assert stats["captions"] == 0
    assert "\x00" not in raw["content_text"], "占位符必须被清干净"
    assert "这是正文" in raw["content_text"], "正文本身必须完好"


# ---------------------------------------------------------------------------
# 10. 落库安全：raw_metadata 里不能有 NUL（JSONB 不接受 \u0000）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_落库的_raw_metadata_里没有_nul(fake_provider):
    """PostgreSQL 的 JSONB 不接受 `\\u0000`（实测 "unsupported Unicode escape
    sequence"）⇒ 整条建库会炸。落库的记录必须用**可见记法**存占位符。"""
    html = (
        "<!DOCTYPE html><html><head><title>图表文章</title></head><body><article>"
        "<p>" + "这是正文，密度启发式会选中它。" * 20 + "</p>"
        '<img src="https://cdn.example.com/fig1.png" alt="图">'
        "</article></body></html>"
    )
    transport = _transport(article_html=html, article_url=GENERIC_URL)
    fetcher = generic_mod.GenericURLFetcher(transport=transport)
    result = await fetcher.fetch(GENERIC_URL)

    # 落库形态：正文里的占位符是**可见记法**（JSONB 能存），不是裸 NUL
    stored = _stored(result)
    assert "\\x00IMG1\\x00" in stored["content_text"]
    assert "\x00" not in stored["content_text"]

    raw, _ = await media_ocr.caption_stored_content(stored, transport=transport)

    dumped = json.dumps(raw["raw_metadata"], ensure_ascii=False)
    assert "\x00" not in dumped
    assert "\\u0000" not in dumped
    # 占位符以**可见记法**落库（信息不丢，也能对上号）
    records = raw["raw_metadata"]["media_captions"]
    assert records[0]["token"] == "\\x00IMG1\\x00"
    # 补完图注后正文里不再有占位符（已被图注替掉）
    assert "\x00" not in raw["content_text"]


@pytest.mark.asyncio
async def test_落库形态的图清单_不含_nul(fake_provider):
    """解析阶段留下的 `media_refs` 也不能带 NUL（它同样会进 raw_metadata）。"""
    html = (
        "<!DOCTYPE html><html><head><title>图表文章</title></head><body><article>"
        "<p>" + "这是正文，密度启发式会选中它。" * 20 + "</p>"
        '<img src="https://cdn.example.com/fig1.png" alt="图">'
        "</article></body></html>"
    )
    fetcher = generic_mod.GenericURLFetcher(transport=httpx.MockTransport(_transport_handler))
    # 直接走解析入口（不经过 fetch 的识图步骤），检查中间态
    acq = _acquired(html)
    result = fetcher.parse(acq)

    dumped = json.dumps(result.raw_metadata, ensure_ascii=False)
    assert "\x00" not in dumped
    assert result.raw_metadata["media_refs"] == [
        {"url": "https://cdn.example.com/fig1.png", "index": 1}
    ]
    # 正文里的占位符本身带 NUL（那是内存里的形态），但记录里没有
    assert "\x00IMG1\x00" in result.content_text


def _transport_handler(request: httpx.Request) -> httpx.Response:
    """解析阶段用不到网络（这里只是占位，避免误发真实请求）。"""
    raise AssertionError("解析阶段不该发网络请求")


def _acquired(html: str):
    """构造 pipeline 交给 parse 的 Acquired（用真 dataclass）。"""
    import importlib as _importlib

    pipeline = _importlib.import_module("cs_media_fetchers.pipeline")
    return pipeline.Acquired(
        html=html,
        final_url=GENERIC_URL,
        status=200,
        tier="http",
        url=GENERIC_URL,
    )


# ---------------------------------------------------------------------------
# 9. 抓取总预算：识图不许把用户的等待时间撑爆
# ---------------------------------------------------------------------------
#
# 剪藏是**同步接口**（`_prefetch_for_capture` 在请求线程里 await 抓取），
# 总时限 30s 是硬约束（见 `content-service/main.py::_capture_fetch_budget`
# 与 `fetchers/net.py::Deadline` 的说明）。
#
# 识图是「网络 + 模型」调用，最坏 15s 下载 + 5×15s 串行识图 = 90s
# ⇒ 如果不接进同一个 deadline，用户就得多等一分半。
#
# ⇒ 判据：预算耗尽 ⇒ 剩下的图**直接放弃**（占位符随后被删掉，正文完整）。
# 「宁可这一篇没有图注，也不能让用户多等」。


@pytest.mark.asyncio
async def test_抓取预算耗尽_图被放弃且正文不留残迹(fake_provider):
    refs = _refs(3)
    deadline = net.Deadline(0.0)  # 已耗尽
    captions = await media_ocr.caption_media(refs, transport=_transport(), deadline=deadline)
    assert len(captions) == 3
    assert all(c["ok"] is False for c in captions)
    assert all("预算" in (c["error"] or "") for c in captions)

    text = media_ocr.apply_captions("前文 \x00IMG1\x00 \x00IMG2\x00 \x00IMG3\x00 后文", captions)
    assert "\x00" not in text
    assert "前文" in text and "后文" in text


@pytest.mark.asyncio
async def test_抓取预算充足_正常识图(fake_provider):
    refs = _refs(1)
    deadline = net.Deadline(60.0)
    captions = await media_ocr.caption_media(refs, transport=_transport(), deadline=deadline)
    assert captions[0]["ok"] is True
    assert DEFAULT_CAPTION in captions[0]["caption"]


@pytest.mark.asyncio
async def test_识图超时取剩余预算而不是固定值(fake_provider, monkeypatch):
    """⚠️ 超时必须随剩余预算收敛，否则慢的抓取会把总时长撑爆。"""
    seen_timeouts: list[float] = []

    async def _fake_ocr(blob, mime, *, prompt=None, timeout=None, transport=None):
        seen_timeouts.append(timeout)
        return DEFAULT_CAPTION

    monkeypatch.setattr(media_ocr, "ocr_image_bytes", _fake_ocr)
    deadline = net.Deadline(3.0)
    await media_ocr.caption_media(_refs(1), transport=_transport(), deadline=deadline)
    assert seen_timeouts
    assert seen_timeouts[0] <= 3.0, "超时不能超过剩余预算"


# ---------------------------------------------------------------------------
# 5. Referer 必须走**请求级**（2026-10-25 修的真 bug）
# ---------------------------------------------------------------------------
#
# 现象：sspai 的一篇文章 47 张正文图**全部** 403（"下载失败: http 403"），
# 而用独立 client 手工测同一个 URL 是 200。
#
# 根因：client 是**池化**的，池的 key 是 (画像, 代理, guard_ssrf) —— **不含 headers**
# （见 net.acquire_client）。⇒ 第一次用某画像建 client 时它当时的 headers 被
# "烧"进 client 级，之后同画像的请求**复用**它 ⇒ **新传的 client 级 headers
# 被静默丢弃**。
#
# 于是：页面抓取（不带 Referer）先建了 DESKTOP client ⇒ 识图下载时传的
# `Referer: <文章URL>`（per-article！）**必然丢失** ⇒ 图床防盗链回 403。
#
# 微信为什么没事：它的 Referer 在**画像**里（固定的）⇒ client 级，不受影响。


def test_池复用时后来的client级headers会被丢弃():
    """把**机制**钉住：池的 key 不含 headers ⇒ 后来的 client 级 headers 失效。

    这条测试看着"在测一个 bug"，其实是在说明
    「为什么 per-request 的头不能放 client 级」——没有它，
    下一个人会理所当然地把 Referer 塞进 client 级。
    """
    net.reset_clients_for_tests()
    try:
        c1 = net.acquire_client(
            net.HeaderProfile.DESKTOP, headers={"X-Test": "first"}, trust_env=False
        )
        c2 = net.acquire_client(
            net.HeaderProfile.DESKTOP, headers={"X-Test": "second"}, trust_env=False
        )
        assert c1 is c2, "同画像应该复用同一个 client"
        # ⚠️ 这就是坑：第二个调用方传的 client 级 header **被丢弃**了
        assert c2.headers["x-test"] == "first"
    finally:
        net.reset_clients_for_tests()


@pytest.mark.asyncio
async def test_识图下载的请求带文章Referer(fake_provider):
    """效果：图下载请求必须**带**上文章 Referer（防盗链的钥匙）。"""
    seen: dict[str, httpx.Headers] = {}
    refs = _refs(1)
    await media_ocr.caption_media(
        refs,
        transport=_transport(seen=seen),
        referer="https://sspai.com/post/90211",
    )
    img_headers = seen["https://cdn.example.com/fig1.png"]
    assert img_headers.get("referer") == "https://sspai.com/post/90211"


@pytest.mark.asyncio
async def test_识图不把Referer放进client级headers(monkeypatch, fake_provider):
    """修法本身：**Referer 不许进 client 级 headers**。

    进了就会在池复用时被丢弃（见上面那条）⇒ 表现为「图床 403」，
    而表面上看代码是"传了 Referer 的"，极难定位。
    """
    captured: dict = {}
    real = media_ocr.build_client_kwargs

    def _spy(profile, **kwargs):
        captured.update(kwargs)
        return real(profile, **kwargs)

    monkeypatch.setattr(media_ocr, "build_client_kwargs", _spy)

    await media_ocr.caption_media(
        _refs(1),
        transport=_transport(),
        referer="https://sspai.com/post/90211",
    )

    client_headers = {k.lower() for k in (captured.get("headers") or {})}
    assert "referer" not in client_headers, (
        "Referer 不能进 client 级 headers —— 池的 key 不含 headers，"
        "它会被「首个使用者」的值覆盖掉"
    )


# ---------------------------------------------------------------------------
# 11. A. 按 URL 去重（2026-10-25）
# ---------------------------------------------------------------------------
#
# 同一张图在一篇里出现几次（重复插图、站点自己的图标）**只需要识别一次** ——
# 之前是"出现几次就识几次"，白烧几份 token，还把图床的耐心耗掉了。
#
# 注意：**占位符不能去重**（每张图在正文里的位置不同），
# 去重只发生在"下载 + 识图"那一层，图注要**复制**给每个占位符。


def _figure_transport(urls: list[str]) -> httpx.MockTransport:
    """每张图给**不同的字节**，字节里带上自己的编号（假识图靠它认自己）。"""

    bodies = {url: PNG_BYTES + f"FIG{i + 1}".encode() for i, url in enumerate(urls)}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": DEFAULT_CAPTION}}]}
            )
        body = bodies.get(str(request.url))
        return httpx.Response(200, content=body or PNG_BYTES)

    return httpx.MockTransport(handler)


def _figure_of(blob: bytes) -> int:
    """从图片字节里认出编号（配 `_figure_transport`）。"""
    raw = blob.decode("latin-1")
    return int("".join(ch for ch in raw.split("FIG", 1)[-1] if ch.isdigit()) or 0)


@pytest.mark.asyncio
async def test_同一个URL只识一次_图注复制给每个占位符(monkeypatch, fake_provider):
    """同一张图出现三次 ⇒ 只 OCR 一次，三个占位符**都**拿到图注。"""
    url = "https://cdn.example.com/same.png"
    refs = [{"url": url, "token": f"\x00IMG{i}\x00"} for i in (1, 2, 3)]
    calls = {"n": 0}

    async def _fake_ocr(blob, mime, *, prompt=None, timeout=None, transport=None):
        calls["n"] += 1
        return "这张图写着 42"

    monkeypatch.setattr(media_ocr, "ocr_image_bytes", _fake_ocr)

    captions = await media_ocr.caption_media(refs, transport=_transport())

    assert calls["n"] == 1, "同一个 URL 不该识多次（白烧 token）"
    assert len(captions) == 3
    assert all(c["ok"] for c in captions), captions
    assert all(c["caption"] == "这张图写着 42" for c in captions)

    # 三个占位符都被换成图注（复制，不是"只留一个"）
    out = media_ocr.apply_captions("\n\n".join(f"\x00IMG{i}\x00" for i in (1, 2, 3)), captions)
    assert out.count("图表：这张图写着 42") == 3
    assert "\x00" not in out


@pytest.mark.asyncio
async def test_上限数的是不同URL_重复图不额外占名额(monkeypatch, fake_provider):
    """上限按**不同 URL 数**计（不是位置数）。

    A A B C 四张位置、limit=2 ⇒ 识 A 和 B 两个 URL（按位置算的话只识得到 A）。
    """
    urls = [
        "https://cdn.example.com/a.png",
        "https://cdn.example.com/a.png",
        "https://cdn.example.com/b.png",
        "https://cdn.example.com/c.png",
    ]
    refs = [{"url": u, "token": f"\x00IMG{i + 1}\x00"} for i, u in enumerate(urls)]
    seen: list[str] = []

    async def _fake_ocr(blob, mime, *, prompt=None, timeout=None, transport=None):
        return "有内容"

    monkeypatch.setattr(media_ocr, "ocr_image_bytes", _fake_ocr)

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/chat/completions"):
            seen.append(str(request.url))
        return httpx.Response(200, content=PNG_BYTES)

    captions = await media_ocr.caption_media(refs, transport=httpx.MockTransport(handler), limit=2)

    assert sorted(set(seen)) == [
        "https://cdn.example.com/a.png",
        "https://cdn.example.com/b.png",
    ], f"识了不该识的图：{seen}"
    assert captions[0]["ok"] and captions[1]["ok"], "A 的两个位置都要有图注"
    assert captions[2]["ok"], "B 要有图注（重复的 A 不该占掉 B 的名额）"
    assert captions[3]["ok"] is False, "C 超上限"
    assert "超出单篇识图上限" in (captions[3]["error"] or "")


# ---------------------------------------------------------------------------
# 12. B. 零信息判失败（2026-10-25）
# ---------------------------------------------------------------------------
#
# OCR **明确声明「图里没有文字」** ⇒ 这张图没有任何可用信息 ⇒ 判失败。
# 不判的话，正文里会出现"图表：图中无文字与数字内容。"这种纯噪音。


def test_零信息判据只认明确声明_不误杀真有内容的图():
    """判据的形状来自**生产实测**（2026-10-25，sspai 47 张图）。

    - 零信息的主流形态是"图中无文字与数字内容。"（6/47）；
    - 上一版启发式（按长度/画面描述过滤）**误杀**了下面这些**真有文字**的图
      ⇒ 判据只认"明确声明"，不认启发式。
    """
    assert media_ocr._is_zero_info("图中无文字与数字内容。")
    assert media_ocr._is_zero_info("图中没有文字。")
    assert media_ocr._is_zero_info("图片中没有可识别的文字内容。")
    assert media_ocr._is_zero_info("该图片不包含任何文字信息。")

    for text in (
        "10 小米10 新品直播发布会",
        "高端是小米发展的必由之路，更是生死之战！",
        "为小米汽车而战",
        "红色圆形背景中有一个白色的希腊字母π。",
        "工作、消费主义和新穷人",
    ):
        assert not media_ocr._is_zero_info(text), f"误杀了真有内容的图：{text}"


@pytest.mark.asyncio
async def test_零信息图注判失败_不进正文(monkeypatch, fake_provider):
    """零信息 ⇒ 判失败 ⇒ 正文里**不留**"图表：图中无文字"。"""

    async def _fake_ocr(blob, mime, *, prompt=None, timeout=None, transport=None):
        return "图中无文字与数字内容。"

    monkeypatch.setattr(media_ocr, "ocr_image_bytes", _fake_ocr)

    captions = await media_ocr.caption_media(_refs(1), transport=_transport())

    assert captions[0]["ok"] is False
    assert media_ocr.ZERO_INFO_ERROR in (captions[0]["error"] or "")
    out = media_ocr.apply_captions("前文 \x00IMG1\x00 后文", captions)
    assert "图表：" not in out, "零信息不该进正文"
    assert "图中无文字" not in out
    assert "前文" in out and "后文" in out


# ---------------------------------------------------------------------------
# 13. C. 补位（2026-10-25）
# ---------------------------------------------------------------------------
#
# 空出来的名额由**后面的图**补上。
# 闸门（必须）：**最多 1 轮**、**总尝试 ≤ 2×limit**、预算耗尽不补。


@pytest.mark.asyncio
async def test_零信息占掉的名额由后面的图补上(monkeypatch, fake_provider):
    """第一轮零信息占掉的名额 ⇒ 补位轮拿后面的图顶上。"""
    urls = [f"https://cdn.example.com/fig{i}.png" for i in range(1, 6)]
    refs = [{"url": u, "token": f"\x00IMG{i + 1}\x00"} for i, u in enumerate(urls)]
    calls: list[int] = []

    async def _fake_ocr(blob, mime, *, prompt=None, timeout=None, transport=None):
        n = _figure_of(blob)
        calls.append(n)
        if n == 1:
            return "图中无文字与数字内容。"
        return f"图 {n} 的内容"

    monkeypatch.setattr(media_ocr, "ocr_image_bytes", _fake_ocr)

    captions = await media_ocr.caption_media(refs, transport=_figure_transport(urls), limit=2)

    assert sorted(calls) == [1, 2, 3], f"补位轮没跑或跑多了：{calls}"
    assert captions[0]["ok"] is False, "零信息那张仍然是失败"
    assert media_ocr.ZERO_INFO_ERROR in (captions[0]["error"] or "")
    assert captions[1]["ok"] is True
    assert captions[2]["ok"] is True, "补位上来的那张要有图注"
    assert captions[2]["caption"] == "图 3 的内容"
    assert captions[3]["ok"] is False, "没被补到的仍然超上限"
    assert "超出单篇识图上限" in (captions[3]["error"] or "")


@pytest.mark.asyncio
async def test_补位最多一轮_不会一直往下翻图(monkeypatch, fake_provider):
    """闸门：第一轮全失败 ⇒ 只补 1 轮（总尝试 ≤ 2×limit），不会有第三轮。"""
    urls = [f"https://cdn.example.com/fig{i}.png" for i in range(1, 11)]
    refs = [{"url": u, "token": f"\x00IMG{i + 1}\x00"} for i, u in enumerate(urls)]
    calls: list[int] = []

    async def _fake_ocr(blob, mime, *, prompt=None, timeout=None, transport=None):
        calls.append(_figure_of(blob))
        return "图中无文字与数字内容。"

    monkeypatch.setattr(media_ocr, "ocr_image_bytes", _fake_ocr)

    captions = await media_ocr.caption_media(refs, transport=_figure_transport(urls), limit=3)

    assert len(calls) == 6, f"总尝试应 ≤ 2×limit=6，实际 {len(calls)}"
    assert all(c["ok"] is False for c in captions)
    assert all("超出单篇识图上限" in (c["error"] or "") for c in captions[6:])


@pytest.mark.asyncio
async def test_预算耗尽不补位(monkeypatch, fake_provider):
    """剪藏总时限是硬约束 ⇒ 预算耗尽时**不再开新一轮**。"""
    urls = [f"https://cdn.example.com/fig{i}.png" for i in range(1, 7)]
    refs = [{"url": u, "token": f"\x00IMG{i + 1}\x00"} for i, u in enumerate(urls)]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/chat/completions"):
            seen.append(str(request.url))
        return httpx.Response(200, content=PNG_BYTES)

    deadline = net.Deadline(0.0)
    captions = await media_ocr.caption_media(
        refs, transport=httpx.MockTransport(handler), limit=2, deadline=deadline
    )

    assert len(seen) == 0, f"预算耗尽就不该下载：{seen}"
    assert all(c["ok"] is False for c in captions)
    assert all("预算" in (c["error"] or "") for c in captions[:2])


@pytest.mark.asyncio
async def test_补位轮炸了_不毁掉第一轮的图注(monkeypatch, fake_provider):
    """补位是**锦上添花**：它炸了也不能让第一轮已经拿到的图注全丢。"""
    urls = [f"https://cdn.example.com/fig{i}.png" for i in range(1, 5)]
    refs = [{"url": u, "token": f"\x00IMG{i + 1}\x00"} for i, u in enumerate(urls)]

    async def _fake_ocr(blob, mime, *, prompt=None, timeout=None, transport=None):
        n = _figure_of(blob)
        if n == 1:
            return "图中无文字与数字内容。"
        if n == 3:
            raise RuntimeError("补位那一轮炸了")
        return f"图 {n} 的内容"

    monkeypatch.setattr(media_ocr, "ocr_image_bytes", _fake_ocr)

    captions = await media_ocr.caption_media(refs, transport=_figure_transport(urls), limit=2)

    assert captions[1]["ok"] is True, "第一轮成功的图注不能被补位失败带走"
    assert captions[1]["caption"] == "图 2 的内容"
