"""
第三方回调鉴权 —— 共享密钥校验（fail-closed）。

## 为什么需要

`content-service` 的两个回调端点历史上**完全没有鉴权**：

    @app.post("/api/v1/callback/wechat-mp-message")
    async def wechat_mp_message(req: WechatMpMessageRequest, db: ...):
        # 原注释：不需要 JWT（公众号回调，公众号已认证用户身份）

代码注释的理由是「公众号已认证用户身份」，但**代码里从未校验过公众号身份** ——
注释描述的是一个没有落地的假设。后果实测为：

- 互联网上任意匿名请求，`text` 塞任意 URL 即可建文章 + 触发蒸馏
- 该路径明确**不扣配额**（注释原文：「不扣配额（匿名入口，等客户端登录后再扣）」）
- → 未鉴权即可无限烧 LLM / TTS 费用

`clawbot-message` 同理。这两个端点在生产日志里**零流量**，说明从未真正接通，
所以收紧它们不会打断任何在用的链路 —— 现在不关，以后接通时就是裸奔上线。

## 设计：未配置 = 拒绝

这是 fail-closed 的关键取舍。备选方案是「未配置时放行 + 打 warning」，
但那等于把「忘了配」变成「默认不安全」，而这类端点失效时的代价是直接烧钱。
所以 `STASHBOX_CALLBACK_SECRET` 未设置时端点返回 503 并说明原因，
运维想开这个口子必须显式配一个密钥。

## 用法

    from stashbox.backend.common.callback_auth import require_callback_secret

    @app.post("/api/v1/callback/xxx")
    async def xxx(req: XxxRequest, _=Depends(require_callback_secret)): ...

对接真正的微信服务号时应换成官方的 signature/timestamp/nonce 校验
（`sha1(sorted([token, timestamp, nonce]))`）。本模块的共享密钥是对那之前的
最小可用防线，不是微信官方协议的等价实现。
"""

import hmac

from fastapi import Header, HTTPException, status

from stashbox.backend.common.config import settings

#: 回调方用来带密钥的请求头。Authorization 也接受 Bearer 形式，
#: 方便复用现有的 curl / 网关调试习惯。
SECRET_HEADER = "X-Callback-Secret"


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def verify_callback_secret(provided: str | None) -> None:
    """校验回调密钥，不通过则抛 401。"""
    expected = settings.callback_shared_secret
    if not expected:
        # fail-closed：没有配密钥就等于没有防护，此时放行等于裸奔。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"回调端点未配置 {SECRET_HEADER} 服务端密钥，已拒绝。"
                "确需启用请设置环境变量 STASHBOX_CALLBACK_SECRET。"
            ),
        )
    if not provided:
        raise _unauthorized(f"missing {SECRET_HEADER}")
    # 常量时间比较，避免通过响应时间逐字节猜密钥
    if not hmac.compare_digest(provided, expected):
        raise _unauthorized("invalid callback secret")


async def require_callback_secret(
    x_callback_secret: str | None = Header(default=None, alias=SECRET_HEADER),
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI 依赖：把 ``X-Callback-Secret`` 或 ``Authorization: Bearer <secret>``
    校验通过，否则 401（未配置密钥时 503）。

    返回值是校验通过的密钥本身，调用方一般用不到。
    """
    provided = x_callback_secret
    if provided is None and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    verify_callback_secret(provided)
    return provided or ""
