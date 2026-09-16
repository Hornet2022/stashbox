# content-service（端口 8102）

听匣业务层：文章 CRUD + 待听/听过/收藏/跳过 + 主题标签 + D9 回调入口。
CP1.5 起已接 **PostgreSQL（articles 表）**，替换原 in-memory dict。
**OSS 只被本服务写**（全局约束）。

> 端口说明：CP1.5 起默认 **8102**（本机 8002 被其它项目占用，整体平移到 8100 段）。

## API

| 路由 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查 |
| `/api/v1/articles/add` | POST | 加入文章 `{url, source}` |
| `/api/v1/articles/pending` | GET | 当前用户待听列表 |
| `/api/v1/articles/listened` | GET | 听过列表 |
| `/api/v1/articles/{id}` | GET | 文章详情（校验 owner，非 owner 返回 403）|
| `/api/v1/articles/{id}/mark-listened` | POST | 标记听过 |
| `/api/v1/articles/{id}/favorite` | POST | 收藏 |
| `/api/v1/articles/{id}/skip` | POST | 跳过 |
| `/api/v1/callback/d9-add-article` | POST | **D9 入口（核心）** `{url, title?}` |
| `/api/v1/callback/clawbot-message` | POST | ClawBot 入口（mock）|
| `/api/v1/tags` | GET | 主题标签（mock）|
| `/api/v1/admin/stats` | GET | 管理员后台统计 |

> 除 `/health` 外所有接口均需 `Authorization: Bearer <token>`。

## 本地启动

```bash
cd backend/content-service
PYTHONPATH=/Users/hornet/work uvicorn main:app --reload --port 8002
```

## 复用

- `stashbox.backend.common.auth`（`require_user`）
- `stashbox.backend.common.config` / `logging` / `exceptions`（`NotFound` / `Forbidden`）
