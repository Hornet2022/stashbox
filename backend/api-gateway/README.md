# api-gateway（端口 8000）

听匣统一入口：健康检查 + JWT 签发 + 下游路由分发。

## 职责

| 路由 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查 |
| `/api/v1/auth/token` | POST | 签发 JWT（公开，body `{"user_id": "u_test"}`）|
| `/api/v1/{path:path}` | 任意 | 按前缀转发到下游服务 |

## 路由分发规则

| 前缀 | 转发到 |
|---|---|
| `/api/v1/user` `/api/v1/subscription` | `settings.user_service_url`（默认 `http://localhost:8001`）|
| `/api/v1/articles` `/api/v1/tags` `/api/v1/callback` | `settings.content_service_url`（默认 `http://localhost:8002`）|
| `/api/v1/distill` | `settings.ai_service_url`（默认 `http://localhost:8003`）|
| 其他 | 404 |

> 本地开发通过环境变量覆盖下游地址：`USER_SERVICE_URL` / `CONTENT_SERVICE_URL` / `AI_SERVICE_URL`。

## 本地启动

```bash
cd backend/api-gateway
PYTHONPATH=/Users/hornet/work uvicorn main:app --reload --port 8000
```

或通过根目录一键脚本：`bash backend/run_dev.sh`（同时起 4 个服务）。

## 复用

- `stashbox.backend.common.auth`（JWT 签发）
- `stashbox.backend.common.config`（下游地址配置）
- `stashbox.backend.common.logging`（日志）
- `stashbox.backend.common.exceptions`（全局异常）
