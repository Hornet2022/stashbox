# Dev 运行时运维指南（CP10.6 教训沉淀）

## 4 服务端口

| 服务 | 端口 | 启动入口 |
|---|---|---|
| api-gateway | 8100 | `python -m uvicorn api-gateway.main:app --port 8100` |
| user-service | 8101 | `python -m uvicorn main:app --port 8101` |
| content-service | 8102 | `python -m uvicorn main:app --port 8102` |
| ai-service | 8103 | `python -m uvicorn main:app --port 8103` |

## 启动方式（推荐顺序）

### 1. 一键全启动（首选）

```bash
cd /Users/hornet/work/stashbox/backend
./run_dev.sh
```

`run_dev.sh` 内部已经 export `USER_SERVICE_URL` / `CONTENT_SERVICE_URL` / `AI_SERVICE_URL` 三个 env（line 19-21），并顺序启动 4 个 uvicorn + 1 个 arq worker。Ctrl+C 退出时 trap cleanup 杀所有进程。

### 2. 单服务重启（次选）

```bash
cd /Users/hornet/work/stashbox/backend
bash scripts/restart-api-gateway.sh
```

每个服务都有对应 `scripts/restart-<service>.sh` 模板。**为什么单重启要走脚本？** 手动 `nohup python -m uvicorn ... &` 会漏 env，下游 URL 默认走 docker 服务名 `http://user-service:8001` → 502。

## 验证 env 生效

```bash
# 检查 api-gateway 进程的 SERVICE_URL env
ps eww $(lsof -ti :8100) | grep -E "SERVICE_URL"
# 期望输出 3 行:
#   USER_SERVICE_URL=http://localhost:8101
#   CONTENT_SERVICE_URL=http://localhost:8102
#   AI_SERVICE_URL=http://localhost:8103
```

如果只看到 `PYTHONPATH` 等其他 env 看不到 `*_SERVICE_URL`，**说明服务是用手动 nohup 启动的**，必须重启。

## 常见故障

### 症状: 502 Bad Gateway / 转发到 `ai-service` 主机名解析失败

```
httpx.ConnectError: [Errno 8] nodename nor servname provided, or not known
```

**根因**: `AI_SERVICE_URL` env 没生效，pydantic-settings 用了 `common/config.py:45` 的默认 `http://ai-service:8003`。
**修法**: `bash scripts/restart-api-gateway.sh`（会自动 export 三个 URL）。

### 症状: 401 Unauthorized

正常 — JWT 缺失或过期。客户端应走 `POST /api/v1/auth/token` 拿新 token。

### 症状: 403 quota exceeded

```
{"code":3001,"message":"quota exceeded: 5/5"}
```

**根因**: dev 用户 `monthly_quota=5` 满。详见 `docs/dev-quota-reset.md`。

### 症状: 500 Internal Server Error + 看到 stack trace

后端 bug，需要看 `lsof -ti :8100` 对应进程的 stdout/stderr。`run_dev.sh` 默认输出到终端；`scripts/restart-*.sh` 走 `tee` 落到 `/tmp/<service>-restart.log`。

## env 时序陷阱（CP10.6 根因）

**关键事实**: `pydantic-settings` 在 `Settings()` 类实例化时（=模块 import 时）就锁住所有 env。如果你在 `python -m uvicorn api-gateway.main:app` 之后才 `export AI_SERVICE_URL=...`，pydantic 不会重读。

**结论**: 任何手动启动 uvicorn 之前**必须先 export 4 个 URL**（USER/CONTENT/AI/PYTHONPATH）。

`run_dev.sh` 和 `scripts/restart-*.sh` 都已经把这个写对。**唯一会出问题的就是手动 nohup 拼命令**。
