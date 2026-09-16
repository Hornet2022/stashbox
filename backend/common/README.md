# 共享库（stashbox.backend.common）

> 后端 4 个服务共享的基础设施：配置 / 数据库 / 缓存 / 日志 / JWT 鉴权 / 异常 / ORM 模型。

## 模块清单（已实现）

| 模块 | 职责 |
|---|---|
| `config.py` | pydantic-settings 多环境配置 |
| `database.py` | SQLAlchemy 2.0 async engine + session（统一 `Base` 来自 `models.base`）|
| `redis_client.py` | redis-py async 客户端（连接池 + 依赖注入）|
| `logging.py` | 结构化日志（loguru）|
| `auth.py` | JWT 签发 + 解析 + `require_user` 依赖 |
| `exceptions.py` | 业务异常基类 + 全局处理 |
| `models/` | SQLAlchemy ORM 模型子包（`base` / `user` / `article` / `distilled_article`）|

> 注：`schemas.py` / `tracing.py` **尚未实现**（分别在 CP1.6 / CP3 才用），已从清单移除。

## 使用方式

```python
from stashbox.backend.common import settings, get_db
from stashbox.backend.common.auth import require_user
from stashbox.backend.common.models import User, Article, DistilledArticle
```

## 依赖版本

```toml
python = "^3.11"
fastapi = "^0.115"
pydantic = "^2.9"
sqlalchemy = "^2.0"
asyncpg = "^0.30"
redis = "^5.2"
python-jose = "^3.3"
alembic = "^1.14"
```

## 后续演进

- CP1.6：schemas.py（Pydantic 公共 schema）+ 配额扣减事务 + 缓存层
- CP3：tracing.py（OpenTelemetry 初始化）


## CP1.6：quota_service + cache_service

### `quota_service.py` — 配额扣减事务（乐观锁，v1 §4.10）

```python
from stashbox.backend.common import quota_service

await quota_service.consume(db, user_id)   # 扣 1 次；用尽抛 QuotaExceededError(3001/403)
await quota_service.refund(db, user_id)    # 退还（蒸馏失败）：quota_used-1, quota_version+1
await quota_service.get_quota(db, user_id) # 读配额：Redis 缓存 → miss 查 DB + 回填
await quota_service.reset_monthly(db)      # 月度重置（quota_used=0, quota_reset_at=下月 1 号）
asyncio.create_task(quota_service.quota_reset_loop())  # 简单定时器（CP7 换 apscheduler）
```

- 乐观锁：`SELECT ... FOR UPDATE` 取 `quota_version` → `UPDATE ... WHERE id=? AND quota_version=? AND quota_used < monthly_quota`；`rowcount=0` 判定冲突，最多重试 3 次。
- 用尽：`QuotaExceededError`（错误码 3001 / HTTP 403）。
- 依赖 users 表 4 个字段（迁移 `0002_add_quota_fields`）：`monthly_quota` / `quota_used` / `quota_version` / `quota_reset_at`。

### `cache_service.py` — Redis 缓存层（Lua 原子）

| key | 内容 | ttl |
|---|---|---|
| `user:quota:{user_id}` | `{quota_used, monthly_quota, version, reset_at, remaining}` | 60s |
| `article:detail:{id}` | 文章详情 JSON | 300s |
| `user:pending:{user_id}` | 待听列表 JSON list | 60s |

- 扣减后立即 `invalidate_quota(user_id, new_version)`（Lua `EVALSHA`：DEL + 写版本号栅栏 `{key}:inv`）。
- 回填走 `set_quota` Lua：若栅栏版本 > 待写版本则拒绝写入，防止「旧值回填」竞态。
- 脚本未加载时 `NoScriptError` → 自动回退 `EVAL`。
