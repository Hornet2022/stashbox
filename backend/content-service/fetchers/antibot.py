"""人机验证 / 反爬挑战页识别。

## 为什么单独一个模块

抓取失败的原因里有一类很特殊：**目标站没坏、链接也没坏，是它在要求"证明你是人"**。
本机实测百度百家号（4 篇文章全中）：

    纯 HTTP:  baijiahao.baidu.com/s?id=...
              -> 302 -> wappass.baidu.com/static/captcha/tuxing.html
              -> <title>百度安全验证</title>
    无头浏览器: 落到 mbd.baidu.com/newspage/data/error，显示"这里空空如也"

这类失败如果混进 `fetcher.parse`「没能提取出正文」，会造成**三重损失**：

1. 用户被告知"页面可能需要登录后才能查看"，但他根本没机会做什么 —— 他不是要登录，
   是对方要他做人机验证。**给不出下一步的提示等于没提示。**
2. 运维在指标里看到的是 `parse` 失败，只会去查抽取器（而抽取器没问题）。
3. 最贵的一条：这类失败**恰恰是"该不该买住宅代理"这个决策的唯一证据**，
   而它现在被埋在 parse 桶里看不见。所以单独成码不是为了好看，
   是为了让"多少比例的剪藏撞了反爬墙"成为一个能查的数字。

## 不做什么（这条很重要）

**不做验证码破解、不做滑块绕过、不做人机验证的自动求解。**

理由不是"难"，是它**不该做**：

- 那是绕过访问控制，不是我们该做的事；
- 就算做，无头浏览器本身已被识别（实测 5 种 UA/视口组合全部被拦），
  说明对方的风控本来就防的就是这一类，换姿势只会掉进更麻烦的对抗；
- 它是一场我们赢不了的军备竞赛，而军备竞赛的成本最后会转嫁到用户等待时间上。

正确的应对只有三条，每条都不是破解：
1. 诚实告诉用户"这个网站要求人机验证"（本模块的职责）；
2. 让它成为一个可度量的类别（`fetcher.bot_challenge`），供"要不要上代理"决策；
3. 真要提高这一类的成功率，路径是**合法的真实用户会话**或**住宅代理**，
   而不是绕过验证 —— 见 `STASHBOX_FETCH_PROXY`。
"""

from __future__ import annotations

import re

from .base import FetcherError, FetcherErrorCode

#: 已知的人机验证 / 反爬挑战页域名。
#:
#: 用**域名**而不是页面文案做主判据，因为文案会变、域名不会；
#: 而且挑战页通常和文章页在完全不同的域（文章在 baijiahao，验证码在 wappass），
#: 这个跨域跳转本身就是最强的信号 —— 正常文章页不会跳到验证域名。
_CHALLENGE_HOSTS: frozenset[str] = frozenset(
    {
        "wappass.baidu.com",  # 百度安全验证
        "verify.baidu.com",
        "passport.baidu.com",  # 登录/验证中转
    }
)

#: `<title>` 里的挑战页标题。刻意只匹配 **title 标签**而不是整页：
#: 正文页的 JS 载荷里同样可能带"安全验证"这类词（全页匹配会误判，
#: 这正是 wechat `_check_wechat_block` 已经踩过的坑）。
_TITLE_CHALLENGE_RE = re.compile(
    r"<title[^>]*>[^<]*(安全验证|人机验证|滑动验证|验证码|robot check|are you a human"
    r"|unusual traffic|access denied|just a moment)",
    re.IGNORECASE,
)

#: 可见文本里的挑战提示（剔掉 script/style 之后再匹配，理由同上）
_VISIBLE_CHALLENGE_RE = re.compile(
    r"(请完成.{0,6}验证|请输入验证码|滑动验证|安全验证|访问被拒绝|"
    r"your browser|unusual traffic)",
    re.IGNORECASE,
)
_NONVISUAL_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def detect_bot_challenge(html: str, *, final_url: str, source: str) -> None:
    """命中人机验证 / 反爬挑战页 → 抛 FetcherError(BOT_CHALLENGE)。

    判据按可靠性从高到低：

    1. **落地域名**是已知验证域名 —— 最强信号。文章域名与验证域名不同，
       跨域跳转本身就是"对方要求验证"的直接证据。
    2. **`<title>`** 含验证页标题 —— 次强。限定在 title 标签内，
       避免正文页 webpack/JS 载荷里同样的词造成误判。
    3. **可见文本**含挑战提示 —— 最弱，只在前两条都没命中时兜。
       匹配前先剔 script/style，和微信那边的教训是同一件事。

    Args:
        html: 该通道拿到的 HTML
        final_url: 重定向之后的最终 URL（挑战页往往就在这里露馅）
        source: 报错用的 source 名

    Raises:
        FetcherError(code=BOT_CHALLENGE)
    """
    host = ""
    try:
        host = final_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    except (IndexError, AttributeError):
        host = ""

    if host in _CHALLENGE_HOSTS:
        raise FetcherError(
            code=FetcherErrorCode.BOT_CHALLENGE,
            message=f"landed on anti-bot challenge host: {host}",
            source=source,
        )

    if _TITLE_CHALLENGE_RE.search(html):
        raise FetcherError(
            code=FetcherErrorCode.BOT_CHALLENGE,
            message="anti-bot challenge page (title marker)",
            source=source,
        )

    visible = _NONVISUAL_RE.sub(" ", html)
    if _VISIBLE_CHALLENGE_RE.search(visible):
        raise FetcherError(
            code=FetcherErrorCode.BOT_CHALLENGE,
            message="anti-bot challenge page (visible text marker)",
            source=source,
        )


__all__ = ["detect_bot_challenge"]
