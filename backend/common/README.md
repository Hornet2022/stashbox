# 共享库（stashbox.backend.common）

> 后端 4 个服务共享的基础设施：配置 / 数据库 / 日志 / JWT 鉴权 / OpenTelemetry。

## 模块清单

| 模块 | 职责 |
|---|---|
| `config.py` | pydantic-settings 多环境配置 |
| `database.py` | SQLAlchemy 2.0 async engine + session |
| `redis_client.py` | redis-py async 客户端 |
| `logging.py` | 结构化日志（JSON / console）|
| `auth.py` | JWT 签发 + 解析 + 装饰器 |
| `exceptions.py` | 业务异常基类 + 全局处理 |
| `models.py` | SQLAlchemy 公共基类 |
| `schemas.py` | Pydantic 公共 schema |
| `tracing.py` | OpenTelemetry 初始化 |

## 使用方式

```python
from stashbox.backend.common import settings, get_db, get_redis
from stashbox.backend.common.auth import require_user
```

## 依赖版本

```toml
python = "^3.11"
fastapi = "^0.115"
pydantic = "^2.9"
sqlalchemy = "^2.0"
asyncpg = "^0.29"
redis = "^5.0"
python-jose = "^3.3"
```

## 后续演进

- v1.2：加入 OpenTelemetry exporter
- v1.3：加入 Sentry SDK
