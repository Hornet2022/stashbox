# E2E 集成测（CP1.7.2）

D9 全链路端到端脚本：`curl`（模拟客户端）→ **api-gateway:8100** → content-service:8102 → ai-service:8103。

## 跑前准备

```bash
# 1. 起 4 服务（8100-8103）；前台阻塞，另开一个终端或用 nohup
nohup bash backend/run_dev.sh > /tmp/dev.log 2>&1 & disown

# 2. 跑 E2E
bash tests/e2e/d9_flow.sh
# 或
make e2e-d9
```

退出码：全通过 `0`，任一环节失败 `1`（并打印 `❌` 原因）。

## 跑什么

| Step | 请求 | 断言 |
|---|---|---|
| 0 | `GET /healthz`（gateway / content / ai） | 三个服务 200，否则直接退出并提示起服务 |
| 0.5 | 取 JWT | 见下方「测试用户」 |
| 1 | `POST /api/v1/callback/d9-add-article`（gateway 8100，带 JWT + `X-Request-ID`） | HTTP 200 + 返 `article_id`；响应头 `X-Request-ID` 与请求一致 |
| 2 | `GET /api/v1/articles/{id}/status` | 返 `status` 字段（pending / distilling） |
| 3 | 轮询 status，2s 一次，最多 15 次（30s） | 变 `ready`；`failed` 或超时 → 失败退出 |
| 4 | `GET /api/v1/articles/{id}/audio-url` | `audio_url` 非空 + 符合 OSS 签名 URL 格式；`expires_at` 在未来 0~3700s 内 |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_URL` | `http://localhost:8100` | 被测入口（默认走 gateway，不直连 content-service） |
| `CONTENT_URL` | `http://localhost:8102` | 健康检查用 |
| `AI_URL` | `http://localhost:8103` | 健康检查用 |
| `USER_URL` | `http://localhost:8101` | 开测试账号用（见下） |
| `E2E_USER_ID` | 空 | 指定已有用户（本地签 JWT）；**留空则每次新开一个用户** |
| `POLL_MAX` / `POLL_INTERVAL` | `15` / `2` | 轮询上限（次数 × 秒） |

## 测试用户（为什么默认每次新开）

D9 会走配额预扣（`quota_service.consume`），免费档 5 篇/月。固定用 user 1 跑 5 次就
`3001 quota exceeded`，脚本变成一次性玩具。所以默认路径是：

- `E2E_USER_ID` 留空 → `POST $USER_URL/api/v1/auth/wechat-login`（mock，code 派生 open_id）
  新开一个用户 → 每次跑都有满额 5 篇配额，脚本可无限重跑。
- 指定 `E2E_USER_ID=N` → 直接用 `create_access_token(N)` 本地签 JWT（调试指定用户时用）。
  配额耗尽会报 `❌ 配额用尽（user_id=N）` 并提示换用户。

## 验收 Checklist

- [x] D9 callback 走 api-gateway (8100) ✓
- [x] 文章创建成功（article_id 返 200）✓
- [x] status 轮询 30s 内 ready（实测 8s）✓
- [x] audio-url 含 OSS 签名 URL + expires_at ✓
- [x] X-Request-ID 链路通（curl 带 → 响应头回同一值）✓
- [x] `make e2e-d9` 一键跑通 ✓

## 已知问题（都不是本脚本的 bug）

1. **api-gateway 用 `functools.partial` 注册的路由不收 JSON body**（CP1.7.1 既有）。
   `POST /api/v1/auth/wechat-login`、`POST /api/v1/articles/add` 等通过 8100 打过去一律
   `422`，body 被当成 `Route` dataclass 解析（fastapi 0.141.1 不再解 partial 的签名）。
   D9 不受影响（它是用普通函数 `proxy_d9` 注册的），所以本脚本的被测链路仍全走 gateway；
   只有「开测试账号」这一步直连 8101。修法：`main.py` 里换成闭包工厂
   `def make_proxy(route): async def _p(request): ...`。**未改业务代码，留给 CP1.8 修。**

2. **`POST /api/v1/users/me/quota/reset-monthly` 返 500**（CP1.6 既有）。
   `quota_service.reset_monthly` 把 tz-aware datetime 写进 `TIMESTAMP WITHOUT TIME ZONE`
   列 → `asyncpg.exceptions.DataError: can't subtract offset-naive and offset-aware datetimes`。
   想手动腾配额只能直接改库或等月度定时器。

3. **匿名 D9 路径没覆盖**：v1 §3.5 匿名文章 `user_id=0`，status 端点 `require_user` 拿不到
   owner，本脚本只测 JWT 路径。

4. **测试数据残留**：每跑一次多 1 个 user + 1 篇 article（url 前缀
   `https://mp.weixin.qq.com/s/e2e_test_`）。CI 里要清理的话：
   ```sql
   DELETE FROM articles WHERE url LIKE 'https://mp.weixin.qq.com/s/e2e_%';
   DELETE FROM users WHERE open_id LIKE 'wx_e2e_%';
   ```

5. **ai-service 是 mock**：4 步 × `asyncio.sleep(2)` = 8s 出 ready。CP3.5 接真 LLM/TTS 后
   大概率要调 `POLL_MAX`（30s → 60s+）。
