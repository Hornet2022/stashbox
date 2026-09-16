# ai-service（端口 8103）

听匣 L4 蒸馏引擎（worker + 任务状态机）。本期为 **mock 流水线**：
用 `asyncio.sleep(2)` 模拟每步耗时，不调真实 LLM / TTS（CP3 才接）。

CP1.5 起蒸馏任务已落 **PostgreSQL（distilled_articles 表）**，状态机 `queued → running → done`。
`/health` 额外返回 `redis` 连通状态（本服务是 Redis Stream 队列的未来消费者）。

> 端口说明：CP1.5 起默认 **8103**（本机 8003 空闲，随整体平移到 8100 段）。

## API

| 路由 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查 |
| `/api/v1/distill/start` | POST | 启动蒸馏 `{article_id, url, title?}`，后台跑 4 步 |
| `/api/v1/distill/{task_id}` | GET | 任务进度（status + progress 0.0-1.0）|

## Mock 4 步（v1 §2.3.1）

| Step | 模型（mock）| progress |
|---|---|---|
| 1 多模态理解 | Qwen2.5-VL | 0.25 |
| 2 听感改写 | Claude Sonnet | 0.50 |
| 3 TTS 合成 | 豆包 TTS | 0.75 |
| 4 音频拼接 | FFmpeg | 1.00 |

完成后 `audio_url = https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com/{article_id}.m4a`。

> 本期为进程内 mock，任务状态存在内存；重启即丢失（CP1.5 接 Redis 队列 + PostgreSQL）。

## 本地启动

```bash
cd backend/ai-service
PYTHONPATH=/Users/hornet/work uvicorn main:app --reload --port 8003
```

## 复用

- `stashbox.backend.common.auth`（`require_user`）
- `stashbox.backend.common.config` / `logging` / `exceptions`（`NotFound` / `Forbidden`）
