"""账号合规组端点测试（2026-10-21）

覆盖：隐私政策正文 / 同意记录 / 注销账号 / 设备注册 / 应用版本。

## 为什么这些值得测

这几个端点承载的是**对外的承诺**，而承诺最容易被"看起来实现了"骗过去：

- **注销账号**：`confirm_text` 校验、匿名化、文章软删、token 立即作废。
  任何一环漏了，用户的"我已注销"就只是一个按钮。
- **同意记录**：合规上必须是**显式**同意；版本号不匹配要拒（否则
  "你同意的是 v2" 可能记成 v1）。
- **设备注册**：推送投递的前提；幂等（同一 token 重注册 = 更新归属）。
- **应用版本**：公开端点，未登录也要能读到。

前置：本机 PG 5432 + Redis 6379 已起，且已 ``alembic upgrade head``。
"""

import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import User

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


user_app = _load_app("_cp_account_user_main", "user-service/main.py")

PRIVACY_URL = "/api/v1/policies/privacy"
CONSENT_URL = "/api/v1/users/me/consent"
DELETE_URL = "/api/v1/users/me/account/delete"
DEVICES_URL = "/api/v1/users/me/devices"
VERSION_URL = "/api/v1/app/version"


async def new_user(nickname: str = "pytest_account") -> tuple[int, str]:
    async with AsyncSessionLocal() as session:
        user = User(
            open_id="cp_acct_" + uuid.uuid4().hex[:24],
            nickname=nickname,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return int(user.id), create_access_token(str(user.id))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=user_app), base_url="http://test")


# ══════════════════════════════════════════════════════════════════════
# 隐私政策正文（公开）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_privacy_policy_is_public_and_has_version():
    """没登录也要能读政策 —— 首次启动的同意页就在登录之前。"""
    async with await _client() as c:
        r = await c.get(PRIVACY_URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"], "必须有版本号，客户端靠它判断要不要重新同意"
    assert body["title"]
    assert len(body["content"]) > 200, "正文不能是空的"


@pytest.mark.asyncio
async def test_privacy_policy_content_matches_version():
    """正文里不能残留别的版本的元信息（改版本忘了改正文会露馅）。"""
    async with await _client() as c:
        body = (await c.get(PRIVACY_URL)).json()
    assert "<!-- version:" not in body["content"], "元信息注释不应出现在正文里"
    assert f"版本：{body['version']}" in body["content"] or body["version"] in body["content"]


# ══════════════════════════════════════════════════════════════════════
# 同意记录
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_consent_defaults_to_not_accepted():
    """没记录 = **没同意**。合规上不能"没记录"就当"同意了"。"""
    uid, token = await new_user()
    async with await _client() as c:
        r = await c.get(CONSENT_URL, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["privacy_policy_accepted"] is False
    assert body["privacy_policy_accepted_at"] is None
    # 已同意的版本 = ""（从没同意过）；当前版本单独给一个字段
    assert body["privacy_policy_version"] == ""
    assert body["current_privacy_policy_version"] == "v1"


@pytest.mark.asyncio
async def test_consent_accept_records_version_and_time():
    uid, token = await new_user()
    async with await _client() as c:
        pol = (await c.get(PRIVACY_URL)).json()
        r = await c.put(
            CONSENT_URL,
            headers=_auth(token),
            json={"privacy_policy_version": pol["version"], "accepted": True},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["privacy_policy_accepted"] is True
    assert body["privacy_policy_version"] == pol["version"]
    assert body["current_privacy_policy_version"] == pol["version"]
    assert body["privacy_policy_accepted_at"]


@pytest.mark.asyncio
async def test_consent_with_wrong_version_is_409():
    """客户端拿旧版本号来"同意"必须被拒 —— 那等于同意了一份用户没看过的政策。"""
    uid, token = await new_user()
    async with await _client() as c:
        r = await c.put(
            CONSENT_URL,
            headers=_auth(token),
            json={"privacy_policy_version": "v0-not-real", "accepted": True},
        )
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == 40900
    assert "version mismatch" in body["message"]


@pytest.mark.asyncio
async def test_consent_can_be_withdrawn():
    """撤回同意是合法操作（合规要求可撤回）。"""
    uid, token = await new_user()
    async with await _client() as c:
        pol = (await c.get(PRIVACY_URL)).json()
        await c.put(
            CONSENT_URL,
            headers=_auth(token),
            json={"privacy_policy_version": pol["version"], "accepted": True},
        )
        r = await c.put(
            CONSENT_URL,
            headers=_auth(token),
            json={"privacy_policy_version": pol["version"], "accepted": False},
        )
    assert r.status_code == 200, r.text
    assert r.json()["privacy_policy_accepted"] is False


# ══════════════════════════════════════════════════════════════════════
# 注销账号
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_requires_exact_confirm_text():
    """确认短语不对 → 400。注销是不可逆操作，不能误触。"""
    uid, token = await new_user()
    async with await _client() as c:
        # 前后空白会被 strip（不区分大小写/繁简对中文无意义），所以带空格是**允许**的
        ok = await c.post(DELETE_URL, headers=_auth(token), json={"confirm_text": "  注销听匣  "})
        assert ok.status_code == 200, f"带空格的确认短语应当被接受：{ok.status_code}"
        # 真正错的短语必须挡住
        uid2, token2 = await new_user()
        for wrong in ("", "注销", "注销听匣听匣", "delete", "退出", "注销账号"):
            r = await c.post(DELETE_URL, headers=_auth(token2), json={"confirm_text": wrong})
            assert r.status_code == 400, f"{wrong!r} 不该通过：{r.status_code}"


@pytest.mark.asyncio
async def test_delete_anonymizes_and_soft_deletes():
    """注销 = 匿名化 + 文章软删 + 同意记录清除。"""
    uid, token = await new_user()
    # 造一篇属于他的文章
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO articles (id, user_id, url, source, title, status, created_at, updated_at) "
                "VALUES (:i, :u, 'https://example.com/x', 'web', 'T', 'pending', now(), now())"
            ),
            {"i": f"art_del_{uuid.uuid4().hex[:12]}", "u": uid},
        )
        await s.commit()

    async with await _client() as c:
        r = await c.post(DELETE_URL, headers=_auth(token), json={"confirm_text": "注销听匣"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] is True
    assert body["already_deleted"] is False
    assert "open_id" in body["anonymized_fields"]
    assert body["articles_soft_deleted"] >= 1

    # 落库校验：用户行软删 + PII 清空 + 文章软删
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT deleted_at IS NOT NULL, open_id, nickname, email IS NULL "
                    "FROM users WHERE id=:i"
                ),
                {"i": uid},
            )
        ).one()
        assert row[0] is True, "users.deleted_at 必须被设置"
        assert row[1].startswith("del_"), f"open_id 必须被替换成不可逆值，实际 {row[1]}"
        assert row[2] == "已注销用户"
        assert row[3] is True
        n_articles = (
            await s.execute(
                text("SELECT count(*) FROM articles WHERE user_id=:u AND deleted_at IS NULL"),
                {"u": uid},
            )
        ).scalar()
        assert n_articles == 0, "用户的文章必须全部软删"
        n_consent = (
            await s.execute(text("SELECT count(*) FROM user_consents WHERE user_id=:u"), {"u": uid})
        ).scalar()
        assert n_consent == 0, "同意记录是个人信息，注销时要清掉"


@pytest.mark.asyncio
async def test_delete_revokes_token_immediately():
    """注销后**旧 token 立刻作废** —— 这是"停止提供服务"的兑现点。"""
    uid, token = await new_user()
    async with await _client() as c:
        # 注销前 token 有效
        assert (await c.get(CONSENT_URL, headers=_auth(token))).status_code == 200
        r = await c.post(DELETE_URL, headers=_auth(token), json={"confirm_text": "注销听匣"})
        assert r.status_code == 200
        # 注销后同一个 token 必须 401
        r2 = await c.get(CONSENT_URL, headers=_auth(token))
        assert r2.status_code == 401, f"注销后旧 token 还能用：{r2.status_code}"


@pytest.mark.asyncio
async def test_delete_is_idempotent():
    """重复注销返回 200 + already_deleted（客户端重试不该看到失败）。"""
    uid, token = await new_user()
    async with await _client() as c:
        first = await c.post(DELETE_URL, headers=_auth(token), json={"confirm_text": "注销听匣"})
        assert first.status_code == 200
        # token 已吊销 → 第二次会 401
        second = await c.post(DELETE_URL, headers=_auth(token), json={"confirm_text": "注销听匣"})
        assert second.status_code == 401


@pytest.mark.asyncio
async def test_deleted_user_cannot_refresh_token():
    """已注销的账号换不出新 token（refresh 端点查库，是最后一道闸）。"""
    uid, token = await new_user()
    refresh = create_access_token(str(uid), extra={"type": "refresh"})
    async with await _client() as c:
        await c.post(DELETE_URL, headers=_auth(token), json={"confirm_text": "注销听匣"})
        r = await c.post("/api/v1/auth/refresh-token", json={"refresh_token": refresh})
    assert r.status_code == 401, f"注销后还能换 token：{r.status_code}"


# ══════════════════════════════════════════════════════════════════════
# 设备注册
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_device_register_and_list():
    uid, token = await new_user()
    tok = "dev_" + uuid.uuid4().hex
    async with await _client() as c:
        r = await c.post(
            DEVICES_URL,
            headers=_auth(token),
            json={"token": tok, "platform": "android", "provider": "none"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"] == tok
        assert body["is_active"] is True

        lst = await c.get(DEVICES_URL, headers=_auth(token))
        assert lst.status_code == 200
        assert any(d["token"] == tok for d in lst.json())


@pytest.mark.asyncio
async def test_device_register_is_idempotent_per_token_provider():
    """同一 (token, provider) 再注册 = 更新，不新增行。"""
    uid, token = await new_user()
    tok = "dev_" + uuid.uuid4().hex
    async with await _client() as c:
        a = await c.post(DEVICES_URL, headers=_auth(token), json={"token": tok, "provider": "none"})
        b = await c.post(DEVICES_URL, headers=_auth(token), json={"token": tok, "provider": "none"})
        assert a.json()["id"] == b.json()["id"], "同一 token 重注册应是同一条"
        lst = (await c.get(DEVICES_URL, headers=_auth(token))).json()
        assert len([d for d in lst if d["token"] == tok]) == 1


@pytest.mark.asyncio
async def test_device_register_moves_device_to_new_owner():
    """换账号登录时，设备归属要跟着走 —— 否则会往别人的设备上推。"""
    uid1, token1 = await new_user()
    uid2, token2 = await new_user()
    tok = "dev_" + uuid.uuid4().hex
    async with await _client() as c:
        await c.post(DEVICES_URL, headers=_auth(token1), json={"token": tok, "provider": "none"})
        await c.post(DEVICES_URL, headers=_auth(token2), json={"token": tok, "provider": "none"})
        l1 = (await c.get(DEVICES_URL, headers=_auth(token1))).json()
        l2 = (await c.get(DEVICES_URL, headers=_auth(token2))).json()
    assert not [d for d in l1 if d["token"] == tok], "旧账号不该还持有这台设备"
    assert [d for d in l2 if d["token"] == tok], "新账号应该拿到这台设备"


@pytest.mark.asyncio
async def test_device_unregister():
    uid, token = await new_user()
    tok = "dev_" + uuid.uuid4().hex
    async with await _client() as c:
        created = (
            await c.post(DEVICES_URL, headers=_auth(token), json={"token": tok, "provider": "none"})
        ).json()
        r = await c.delete(f"{DEVICES_URL}/{created['id']}", headers=_auth(token))
        assert r.status_code == 200
        lst = (await c.get(DEVICES_URL, headers=_auth(token))).json()
    assert not [d for d in lst if d["id"] == created["id"]]


@pytest.mark.asyncio
async def test_delete_account_disables_devices():
    """注销要停用设备令牌 —— 共享手机不能继续往旧账号推。"""
    uid, token = await new_user()
    tok = "dev_" + uuid.uuid4().hex
    async with await _client() as c:
        await c.post(DEVICES_URL, headers=_auth(token), json={"token": tok, "provider": "none"})
        await c.post(DELETE_URL, headers=_auth(token), json={"confirm_text": "注销听匣"})
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT is_active, deleted_at IS NOT NULL FROM device_tokens WHERE user_id=:u"
                ),
                {"u": uid},
            )
        ).one()
    assert row[0] is False, "注销后设备令牌必须停用"
    assert row[1] is True


# ══════════════════════════════════════════════════════════════════════
# 应用版本
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_app_version_is_public():
    """更新检查发生在用户还没登录的时候 → 公开端点。"""
    async with await _client() as c:
        r = await c.get(VERSION_URL)
    assert r.status_code == 200, r.text
    body = r.json()
    for k in (
        "latest_version_code",
        "latest_version_name",
        "min_supported_version_code",
        "download_url",
        "release_notes",
        "mandatory",
    ):
        assert k in body, f"缺字段 {k}"
