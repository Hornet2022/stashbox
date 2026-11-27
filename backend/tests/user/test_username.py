"""唯一用户名（username）契约 —— 2026-11-27

背景：`users.nickname` 是**显示名**，微信登录建号时是常量「听友」
⇒ 所有内测账号昵称都一样，而 users 表既没有唯一约束也没有 nickname 索引
⇒ 管理后台分不出「是哪一个」。⇒ 新增 `username`（唯一用户名），
取值规则见 `common/username.py`（单一真相源）。

本文件锁住：

  A. 纯函数：机型/指纹规范化、取值规则、候选序列（不依赖 DB）
  B. DB 层：**唯一索引真的挡住重复**，且**大小写不敏感**
     （⚠️ 索引是迁移里用 `lower(username)` 表达式建的，ORM `create_all`
       建不出来 ⇒ 这里必须**显式建**，否则本组用例是假绿）
  C. 登录建号：带设备特征 ⇒ 设备形态用户名；不带 ⇒ 回落 `t-<id>`
  D. 同一台设备重复登录 ⇒ **用户名不变**（稳定，不因重登漂移）
  D2. 同设备建了**两个账号** ⇒ 第二个自动降级 `-2`（唯一索引冲突兜底）
  E. admin 列表能按用户名搜到（昵称全是同名时唯一的区分手段）
  F. `/api/v1/user` 与 `/api/v1/auth/me` 都返回用户名（两端口径统一）

⚠️ **用例不依赖"库是干净的"**：测试库是**持久**的（跨轮次不清），
如果用例写死设备指纹，上一轮跑出来的账号会占着这个名字 ⇒ 这一轮登录
拿到的就是 `-2`/`-3` ⇒ 用例变成"测库脏不脏"而不是"测契约"。
⇒ 每条需要设备指纹的用例都用**当轮随机**的指纹，期望值用 `build_username`
算出来（既钉死形态，也钉死"同形态同来源 ⇒ 同名"）。

前置：本机 PG 5432 + Redis 6379 已起。
"""

import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal, engine
from stashbox.backend.common.models import User
from stashbox.backend.common.models.base import Base
from stashbox.backend.common.username import (
    build_username,
    is_valid_username,
    normalize_fingerprint,
    normalize_model,
    username_candidates,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    """user-service 目录名带连字符，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


user_app = _load_app("_cp_username_user_main", "user-service/main.py")


@pytest.fixture(autouse=True)
async def _ensure_schema():
    """幂等保证：users 表有 `username` 列 + 大小写不敏感的唯一索引。

    为什么不能只靠 `create_all`
    --------------------------
    测试库由 `scripts/ensure_test_db.py` 跑**真实迁移**建好（0043 里才有这一列
    和这个表达式索引），但 `create_all` **不会给已存在的表加列**、
    也建不出 `lower(username)` 这种表达式索引 ⇒ 库是旧结构时，
    用例会在"缺列"上真红，或在"没索引"上**假绿**（B 组用例会以为唯一性有保障）。

    ⇒ 三件事都显式做一遍（全部幂等）：
      1. `create_all`（表不存在时建表）
      2. `ADD COLUMN IF NOT EXISTS` + 空值回填（`t-<id>`）
      3. `CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_lower`
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[User.__table__])
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "username VARCHAR(32) NOT NULL DEFAULT ''"
            )
        )
        await conn.execute(
            text(
                "UPDATE users SET username = 't-' || id::text "
                "WHERE username IS NULL OR username = ''"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_lower "
                "ON users (lower(username))"
            )
        )
    yield


async def _make_user(
    nickname: str = "听友", username: str | None = None, tier: str = "free"
) -> int:
    async with AsyncSessionLocal() as s:
        u = User(
            open_id="cp_uname_" + uuid.uuid4().hex[:24],
            nickname=nickname,
            username=username or ("t-" + uuid.uuid4().hex[:12]),
            tier=tier,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


def _admin_token(uid: int, tier: str = "admin") -> str:
    return create_access_token(str(uid), extra={"tier": tier})


def _fresh_device_id() -> str:
    """当轮随机的设备指纹（模拟安卓 `and-<hash>` 上报形态）。

    用随机值而不是写死常量：测试库是持久的，写死值会让上一轮跑出来的账号
    占着同一个用户名 ⇒ 这一轮登录拿到的是 `-2` ⇒ 用例测的是"库脏不脏"。
    """
    return "and-" + uuid.uuid4().hex[:12]


async def _username_of(uid) -> str | None:
    """按用户 id 读库里的 username（uid 必须转 int —— 响应里是字符串）。"""
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(text("select username from users where id=:i"), {"i": int(uid)})
        ).scalar()


# ==========================================================================
# A. 纯函数：取值规则（单一真相源 = common/username.py）
# ==========================================================================


def test_机型规范化去噪并截断():
    # 去空格 + 转大写 + 截断到 10 位（MODEL_PART_LEN）。
    assert normalize_model("Xiaomi 14 Pro") == "XIAOMI14PR"
    assert normalize_model("Redmi K80 5G") == "REDMIK805G"
    assert normalize_model("HUAWEI Mate 60 Pro") == "HUAWEIMATE"
    assert normalize_model(None) == ""
    assert normalize_model("") == ""


def test_指纹规范化去来源前缀取开头并大写():
    """去 `and-`/`rnd-` 前缀后取**开头** 8 位。

    为什么是"开头"不是"末位"：三种上报形态的**本体长度不同**
    （`and-`12 位 / `rnd-`16 位 / 裸 12 位）⇒ 按长度取位会漂移，
    同一台设备换一种上报形态就换名 ⇒ "同一台设备恒定同名"这条契约会破。
    """
    assert normalize_fingerprint("and-3f9a2c71deadbeef") == "3F9A2C71"
    assert normalize_fingerprint("rnd-0123456789abcdef") == "01234567"
    assert normalize_fingerprint(None) == ""
    assert normalize_fingerprint("") == ""


def test_同一设备不同上报形态得到同一指纹():
    """同一台设备：`and-<12位>` / 裸 `<12位>` ⇒ 规范化后**必须**同一个指纹。

    这条是"同一台设备恒定同一个 username"的**前提** ——
    安卓端上报的是 `androidIdSuffix() ?: analyticsDeviceId()`，
    前者裸 12 位、后者 `and-` 开头 ⇒ 两种形态都要收敛到同一个值。
    """
    body = uuid.uuid4().hex[:12]
    assert normalize_fingerprint("and-" + body) == normalize_fingerprint(body)


def test_设备形态用户名():
    """有设备特征 ⇒ `d-<机型>_<指纹>`（`d-` 前缀标明来源是设备派生）。"""
    got = build_username("and-3f9a2c71", "Xiaomi 14 Pro")
    assert got == "d-XIAOMI14PR_3F9A2C71"


def test_缺信息回落用户id形态():
    """取不到设备信息 ⇒ `t-<id>`（t = 兜底），保证非空且唯一。"""
    assert build_username(None, None, 23) == "t-23"
    assert build_username("", "", 7) == "t-7"


def test_候选序列冲突时逐个降级():
    """候选顺序：设备形态 → -2 → -3 … → 兜底 t-<id>。"""
    cands = username_candidates("and-3f9a2c71", "Xiaomi 14 Pro", 23, max_attempts=3)
    assert cands[0] == "d-XIAOMI14PR_3F9A2C71"
    assert cands[1] == "d-XIAOMI14PR_3F9A2C71-2"
    assert cands[-1] == "t-23"


def test_用户名合法性校验():
    assert is_valid_username("d-XIAOMI14PR_3F9A2C71")
    assert is_valid_username("t-23")
    assert not is_valid_username("")
    assert not is_valid_username(None)
    assert not is_valid_username("-abc")  # 不能以分隔符开头
    assert not is_valid_username("abc-")  # 不能以分隔符结尾
    assert not is_valid_username("a" * 33)  # 超长
    assert not is_valid_username("a b")  # 含空格


# ==========================================================================
# B. DB 层：唯一索引真的挡住重复（含大小写不敏感）
# ==========================================================================


@pytest.mark.asyncio
async def test_唯一索引拒绝重复用户名():
    """同 username 两次插入 ⇒ IntegrityError（唯一性由 DB 保证，不靠应用层自觉）。"""
    uname = "d-XIAOMI14P_" + uuid.uuid4().hex[:8].upper()
    await _make_user(username=uname)
    with pytest.raises(IntegrityError):
        await _make_user(username=uname)
    # 事务已 aborted ⇒ 用新会话继续，不污染后续用例
    async with AsyncSessionLocal() as s:
        await s.rollback()


@pytest.mark.asyncio
async def test_唯一索引大小写不敏感():
    """`ABC` 与 `abc` 视为同一个 ⇒ 只允许一个入库。

    为什么这条重要：username 语义上大小写不敏感，普通 unique 索引会让
    两者同时入库 ⇒ "唯一"名存形同虚设（迁移 0043 用 lower(username) 表达式索引）。
    """
    suffix = uuid.uuid4().hex[:8].upper()
    await _make_user(username=f"d-TEST_{suffix}")
    with pytest.raises(IntegrityError):
        await _make_user(username=f"d-test_{suffix.lower()}")
    async with AsyncSessionLocal() as s:
        await s.rollback()


# ==========================================================================
# C/D. 登录建号：设备形态 / 回落形态 / 稳定性
# ==========================================================================


async def _login(client, code: str, device_id: str | None, device_model: str | None):
    payload = {"code": code}
    if device_id is not None:
        payload["device_id"] = device_id
    if device_model is not None:
        payload["device_model"] = device_model
    # 每个用例用独立 IP —— 登录端点按 IP 限流，同 IP 连打会撞 429。
    return await client.post(
        "/api/v1/auth/wechat-login",
        json=payload,
        headers={"X-Forwarded-For": f"10.99.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"},
    )


@pytest.mark.asyncio
async def test_登录带设备特征得到设备形态用户名():
    """客户端上报设备特征 ⇒ 新账号的 username 是 `d-<机型>_<指纹>`。"""
    code = "dev_" + uuid.uuid4().hex[:16]
    device_id = _fresh_device_id()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://user-service"
    ) as client:
        resp = await _login(client, code, device_id, "Xiaomi 14 Pro")
    assert resp.status_code == 200, resp.text
    uid = resp.json()["user_id"]
    assert await _username_of(uid) == build_username(device_id, "Xiaomi 14 Pro")


@pytest.mark.asyncio
async def test_登录不带设备信息回落id形态():
    """旧客户端不传设备信息 ⇒ 回落 `t-<id>`，唯一性照样成立（不留 null）。"""
    code = "dev_" + uuid.uuid4().hex[:16]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://user-service"
    ) as client:
        resp = await _login(client, code, None, None)
    assert resp.status_code == 200, resp.text
    uid = resp.json()["user_id"]
    assert await _username_of(uid) == f"t-{int(uid)}"


@pytest.mark.asyncio
async def test_同一设备重复登录用户名不变():
    """同一台设备重复登录 ⇒ **同一个用户、同一个用户名**（不因重登漂移）。"""
    code = "dev_" + uuid.uuid4().hex[:16]
    device_id = _fresh_device_id()
    names = []
    ids = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://user-service"
    ) as client:
        for _ in range(2):
            resp = await _login(client, code, device_id, "Xiaomi 14 Pro")
            assert resp.status_code == 200, resp.text
            ids.append(resp.json()["user_id"])
    async with AsyncSessionLocal() as s:
        for uid in ids:
            names.append(
                (
                    await s.execute(text("select username from users where id=:i"), {"i": int(uid)})
                ).scalar()
            )
    assert ids[0] == ids[1], "同一设备应命中同一个账号"
    expected = build_username(device_id, "Xiaomi 14 Pro")
    assert names[0] == names[1] == expected


# ==========================================================================
# D2. 唯一索引冲突 → 自动降级 -2（SAVEPOINT 重试路径）
# ==========================================================================


@pytest.mark.asyncio
async def test_同设备两个账号第二个自动降级():
    """同设备 + 不同 code ⇒ 两个账号 ⇒ 第二个拿不到同名 ⇒ 自动降到 `-2`。

    这条覆盖 `assign_username` 的 SAVEPOINT 重试路径：
    唯一索引冲突会把事务打成 aborted ⇒ 必须用 `begin_nested()` 发 SAVEPOINT，
    冲突只回滚到保存点，外层事务（用户的 INSERT）才能活着换下一个候选。
    """
    device_id = _fresh_device_id()
    model = "Redmi K80 5G"
    base = build_username(device_id, model)
    ids = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://user-service"
    ) as client:
        for _ in range(2):
            resp = await _login(client, "dev_" + uuid.uuid4().hex[:16], device_id, model)
            assert resp.status_code == 200, resp.text
            ids.append(resp.json()["user_id"])
    assert ids[0] != ids[1], "不同 code 应建出两个账号"
    names = [await _username_of(uid) for uid in ids]
    assert names[0] == base
    assert names[1] == base + "-2", f"第二个账号应自动降级：{names}"


# ==========================================================================
# E. admin 列表能按用户名搜到
# ==========================================================================


@pytest.mark.asyncio
async def test_admin列表能按用户名搜索():
    """昵称全是同名时，唯一用户名是运营唯一的区分手段 ⇒ 必须能按它搜到。"""
    admin_id = await _make_user(nickname="admin_for_search", tier="admin")
    uname = "d-SEARCH_" + uuid.uuid4().hex[:8].upper()
    target_id = await _make_user(nickname="听友", username=uname)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://user-service"
    ) as client:
        resp = await client.get(
            "/api/v1/admin/users",
            params={"keyword": uname},
            headers={"Authorization": f"Bearer {_admin_token(admin_id)}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [it["id"] for it in body["items"]]
    assert target_id in ids, f"按用户名搜不到目标：{body}"
    row = next(it for it in body["items"] if it["id"] == target_id)
    assert row["username"] == uname


# ==========================================================================
# F. 两个 me 端点口径统一（都返回 username）
# ==========================================================================


@pytest.mark.asyncio
async def test_两个me端点都返回用户名且口径一致():
    """`/api/v1/user` 与 `/api/v1/auth/me` 必须给同一个 username。

    为什么钉这条：这两个端点过去给的是**两种不同的昵称兜底**
    （一个 `听友_{id}`、一个 `听友`）⇒ 客户端从不同端点拿到的东西对不上。
    本次只统一**用户名**这一条口径（nickname 是显示名，其兜底差异本次**不改**，
    用户已拍板"默认昵称不动"）。
    """
    uname = "d-MEPORT_" + uuid.uuid4().hex[:8].upper()
    uid = await _make_user(nickname="听友", username=uname)
    token = create_access_token(str(uid))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=user_app), base_url="http://user-service"
    ) as client:
        r1 = await client.get("/api/v1/user", headers={"Authorization": f"Bearer {token}"})
        r2 = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["username"] == uname
    assert r2.json()["username"] == uname
