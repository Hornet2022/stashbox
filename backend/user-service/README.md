# user-service（端口 8101）

听匣用户层：登录（微信 mock）+ 配额 + 订阅定价。

> 端口说明：CP1.5 起默认 **8101**（本机 8001 被其它项目占用，整体平移到 8100 段）。
> CP1.5：wechat-login / user 已接 **PostgreSQL（users 表）**；quota / plans 仍为 mock（CP1.6 接配额扣减事务）。

## API

| 路由 | 方法 | 鉴权 | 说明 |
|---|---|---|---|
| `/health` | GET | 否 | 健康检查 |
| `/api/v1/auth/wechat-login` | POST | 否（公开） | body `{"code":"xxx"}` → 用 code 前 16 位作 user_id 签发 JWT |
| `/api/v1/user` | GET | 是 | 当前用户信息（从 JWT 取 user_id）|
| `/api/v1/user/quota` | GET | 是 | `{plan, total, used, remaining}`（mock：free/5/0/5）|
| `/api/v1/subscription/plans` | GET | 是 | 4 层定价（free/student/member/pro）|

## 本地启动

```bash
cd backend/user-service
PYTHONPATH=/Users/hornet/work uvicorn main:app --reload --port 8101
```

> 或用 `backend/run_dev.sh` 一键起 4 个服务（已自动设置 PYTHONPATH 与端口）。
> 需先起本地依赖（PostgreSQL + Redis）：`docker compose -f infra/docker/docker-compose.dev.yml up -d`

## 复用

- `stashbox.backend.common.auth`（JWT 签发 + `require_user` 依赖）
- `stashbox.backend.common.config` / `logging` / `exceptions`
