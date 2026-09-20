# Dev 配额重置指南（CP10.6 沉淀）

## 当前现状

dev 用户的 `monthly_quota` 默认是 5（CP3.6 时期定的硬编码值），`quota_used` 满后所有 `POST /articles` / `POST /articles/{id}/distill` 都会返 403 + `code=3001`。

```json
{"code":3001,"message":"quota exceeded: 5/5","data":null}
```

**注意**: dev 用 `user 6892` (`wx_test_cp74_dev_user`, APK 默认登录用户) 不在 seed 脚本里 — 它是 `backend/_diag_repro.py` 复现脚本的固定测试 ID，通过 `POST /api/v1/auth/wechat-login` 首次扫码自动建 user。

## 重置方式（推荐用 admin API，不用直接 SQL）

### 1. 拿 admin token

```bash
# POST /api/v1/admin/auth/login 用 open_id='admin_seed' 登录
# 拿到 admin JWT
```

### 2. 调 admin API

```bash
POST /api/v1/admin/users/{user_id}/quota-adjust
Authorization: Bearer <admin_jwt>
Content-Type: application/json

{
  "monthly_quota": 999,
  "reason": "dev testing 配额不足"
}
```

`reason` 至少 5 字符，校验在 `user-service/main.py:567-625`（CP3.6-A2 加的）。调成功后:
- `users.monthly_quota` 立刻改成 999
- `users.quota_used` 不变（admin 显式覆盖 monthly_quota，不动 used）
- 写 `admin_operation_logs` 审计

## 跨月自动重置

`user-service` 启动时拉 `quota_reset_loop`（每小时检查，跨月 `quota_used=0`）。**只重置 used，不动 monthly_quota** — 所以一旦 admin API 改成 999，会一直生效。

## 紧急情况直接 SQL

如果 admin API 不通，紧急直接 SQL:

```python
# 跑一次性 Python 脚本（不要 echo password 到命令行）
import asyncio, asyncpg, re
from common.config import settings  # PYTHONPATH=/Users/hornet/work
url = settings.database_url.replace('postgresql+asyncpg://', 'postgresql://')
m = re.match(r'postgresql:***@]+)@([^:]+):(\d+)/(.+)', url)
user, pw, host, port, db = m.groups()
conn = await asyncpg.connect(host=host, port=int(port), user=user, password=pw, database=db)
await conn.execute("UPDATE users SET monthly_quota = 999, quota_used = 0 WHERE id = 6892")
await conn.close()
```

> ⚠️ **不要把 password echo 到 bash 命令行**。Hermes tool-level safety 会拦。

## 为什么 user 6892 不进 seed 脚本

`alembic/versions/0006_seed_admin_user.py` 是**唯一**的 seed（只 seed `admin_seed`），其他用户（包含 6892）都是**首次微信扫码登录时自动建 user**。这是产品设计选择 — dev 用户的 quota 不应该在每次 alembic 升级时被覆盖。

如果想固化"6892 monthly_quota=999"，**建议**:
- **不要**改 alembic seed 脚本（会污染 alembic 升级链路）
- **应该**走 admin API（`quota-adjust`）一次，之后永久生效
- **或者**改 `common/config.py` 默认 `monthly_quota=5` 改成 999（影响所有 dev 用户）
