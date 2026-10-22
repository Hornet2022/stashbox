"""token 吊销判据的精度语义（2026-10-22）。

## 为什么锁这些

JWT 的 ``iat`` 只有**秒**级精度 —— python-jose 3.5.0 编码时按
``claims[time_claim].utctimetuple()`` 截断（jose/jwt.py:55-58），微秒丢掉。
而吊销时刻是带小数秒的。拿秒级的 ``iat`` 去比一个带小数的边界，
**同一秒内签发的 token 分不出"在吊销前还是吊销后"**。

两种做法都有一边是错的：
- 严 → 登出后**同一秒内**重新登录，新签的 token 被自己的吊销标记拒掉
  （表现为"登录成功但什么功能都 401"，极难排查）；
- 宽 → 登出同一秒签发的**旧** token 还能用满 7 天，"登出"成了假动作。

所以 token 里额外带毫秒戳 ``iat_ms``，吊销边界也按毫秒存，
判据精确到毫秒 —— 两个错都不用犯。

## 本文件锁住

A. 签发早于边界 → 已吊销
B. 签发晚于边界 → 未吊销
C. **同一秒内但晚于边界** → 未吊销（毫秒精度带来的关键好处）
D. 老 token（只有秒级 iat）→ 判定从宽：``iat <= 边界所在秒`` 即已吊销
E. 旧刻度的边界值（秒）→ 照样能读（滚动发布期间 Redis 里可能还有旧值）
"""

from __future__ import annotations

import pytest

from stashbox.backend.common import revocation


@pytest.fixture(autouse=True)
async def _clean():
    """每个用例前后把该用户的吊销标记删掉（测试 Redis 是 db 15）。"""
    yield

    client = revocation._client()
    try:
        await client.delete(revocation._key(999001))
    finally:
        await client.aclose()


async def _revoke_at(uid: int, ts: float) -> None:
    await revocation.revoke(uid, at=ts)


async def test_token_issued_before_boundary_is_revoked():
    uid = 999001
    await _revoke_at(uid, 1_000_000.500)  # 边界 = 1000000500 ms
    assert await revocation.is_revoked({"sub": str(uid), "iat_ms": 1_000_000_000}) is True


async def test_token_issued_after_boundary_is_not_revoked():
    uid = 999001
    await _revoke_at(uid, 1_000_000.500)
    assert await revocation.is_revoked({"sub": str(uid), "iat_ms": 1_000_000_900}) is False


async def test_same_second_but_after_boundary_is_not_revoked():
    """C. 同一秒内、但签发时刻晚于边界 → 不该被吊销。

    这正是"登出后立刻重新登录"能用的原因。判据粗到秒的话，
    这里就会变成 True —— 登录成功但立刻 401。
    """
    uid = 999001
    await _revoke_at(uid, 1_000_000.500)
    # 同一秒（1000000 秒）内，签发于边界之后 100ms
    assert await revocation.is_revoked({"sub": str(uid), "iat_ms": 1_000_000_600}) is False


async def test_legacy_token_without_iat_ms_is_revoked_conservatively():
    """D. 老 token 只有秒级 iat → 宁杀勿放：iat <= 边界所在秒 即已吊销。"""
    uid = 999001
    await _revoke_at(uid, 1_000_000.500)  # 边界所在秒 = 1000000
    assert await revocation.is_revoked({"sub": str(uid), "iat": 1_000_000}) is True
    assert await revocation.is_revoked({"sub": str(uid), "iat": 1_000_001}) is False


async def test_legacy_second_scale_boundary_is_still_understood():
    """E. 滚动发布期间，Redis 里可能还是旧版写的**秒**刻度值。

    旧值：str(1000000.5)；新读法要认出它是秒（×1000），
    而不是当成毫秒（那会差 1000 倍，判据完全失效）。
    """

    client = revocation._client()
    try:
        await client.set(revocation._key(999002), "1000000.5")
    finally:
        await client.aclose()

    try:
        # 边界 = 1000000500 ms
        # 签发于 1000000000 ms → 早于边界 → 已吊销
        assert await revocation.is_revoked({"sub": "999002", "iat_ms": 1_000_000_000}) is True
        # 签发于 1000000900 ms → 晚于边界 → 未吊销
        assert await revocation.is_revoked({"sub": "999002", "iat_ms": 1_000_000_900}) is False
    finally:
        client = revocation._client()
        try:
            await client.delete(revocation._key(999002))
        finally:
            await client.aclose()


def test_parse_boundary_ms_handles_both_scales():
    assert revocation._parse_boundary_ms("ms:1000000500") == 1_000_000_500  # 新格式
    assert revocation._parse_boundary_ms("1000000.5") == 1_000_000_500  # 旧格式（秒）
    assert revocation._parse_boundary_ms("not-a-number") is None
    assert revocation._parse_boundary_ms("0") is None
    assert revocation._parse_boundary_ms("ms:not-a-number") is None
