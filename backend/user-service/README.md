# user-service（端口 8001）

听匣用户层：登录（微信 mock）+ 配额 + 订阅定价。本期为 **in-memory mock**，不连真实 DB。

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
PYTHONPATH=/Users/hornet/work uvicorn main:app --reload --port 8001
```

## 复用

- `stashbox.backend.common.auth`（JWT 签发 + `require_user` 依赖）
- `stashbox.backend.common.config` / `logging` / `exceptions`
