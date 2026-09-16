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

