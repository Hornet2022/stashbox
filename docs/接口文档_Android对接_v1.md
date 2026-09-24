# 听匣后端接口文档 · Android 对接版 v1.1

> 面向 stashbox-android（Kotlin + Compose + Hilt + Media3/ExoPlayer）开发同学。
> 覆盖范围：**听感产品化 CP 系列（CP3.x / CP5.x / CP7.x）后端已落地、Android 可对接的全部接口**。
> 事实来源：`backend/ai-service/main.py`、`backend/content-service/main.py`、`backend/user-service/main.py`、`backend/api-gateway/config.py`（2026-09-24 代码核对，commit `c485866`）。
> 配套文档：《接口文档_管理后台对接_v1.md》（admin-web 侧）。

---

## 0. 全局约定

### 0.1 Base URL 与鉴权

| 环境 | Base URL | 说明 |
|---|---|---|
| dev（模拟器） | `http://10.0.2.2:8100` | Android 模拟器访问宿主 localhost |
| dev（真机） | `http://<局域网IP>:8100` | 与后端约定同网段，禁用 localhost |
| 生产 | 待定 | 一律经 api-gateway（8100），客户端不直连下游服务 |

- 除 `POST /api/v1/auth/token`（mock 签发）和 D9 回调外，所有接口要求 `Authorization: Bearer <JWT>`。
- JWT payload：`sub`（user_id）、`tier`（free/student/member/pro/admin/operator）。
- 网关超时 30s（httpx），客户端建议 read timeout ≥ 35s。

### 0.2 错误信封（全服务统一）

```json
{ "code": 40400, "message": "Resource not found", "data": null }
```

| HTTP | code | 场景 | Android 处理建议 |
|---|---|---|---|
| 400 | 40000 / 4001 | 参数非法（rating 越界、reason 超 64 字符等） | Toast 后端 message |
| 401 | 40100 | JWT 缺失/过期 | 走 refresh-token，失败重登录 |
| 403 | 40300 | 非本人资源 | 不应出现，出现即上报 |
| 404 | 40400 | 资源不存在 / 音频未 ready | 区分处理（见 §2.1） |
| 422 | 42200 | FastAPI 校验失败（`data.errors` 有明细） | debug 构建上报 |
| 429 | 42900 | 配额用尽 | 弹付费墙 |

### 0.3 网关路由可达性（重要）

api-gateway 按 `config.ROUTES` 精确表转发，表外走 fallback **按第一段路径猜下游**：
`articles|tags|callback → content-service`、`distill → ai-service`、`user|subscription → user-service`。

- `GET/POST /api/v1/distill/*`（含本文 §3 变体端点）经 fallback 可达 ai-service ✅
- `POST /api/v1/articles/{id}/rate`、`/listen-complete`、`/skip`、`/progress`、`/feedback-v2` 均在 ROUTES 显式注册 ✅
- ⚠️ 若联调遇到 8100 返回 `404 {"detail":"no downstream route"}`，说明该路径前缀不在猜表内 → 让后端补 ROUTES，**不要直连 8102/8103 绕过网关**。

---

## 1. 蒸馏与播放主链路（既有能力，回顾）

```
添加文章 → 蒸馏 → 轮询状态 → 取播放地址(多码率) → 播放 → 进度上报 → 听完/评分/跳过
```

### 1.1 添加文章（注意：用新端点）

`POST /api/v1/articles` ✅（**新**：扣 1 次配额 + 自动派蒸馏）

```json
// 请求
{ "url": "https://mp.weixin.qq.com/s/xxx", "source": "wechat" }   // source: wechat|douyin|pdf|web|clawbot|wechat_mp
// 响应 200（ArticleResponse）
{ "id": "art_xxx", "url": "...", "title": "...", "source": "wechat", "status": "pending", "created_at": "...", "updated_at": "..." }
```

- ⚠️ 老端点 `POST /api/v1/articles/add` 已标 **Deprecated**（响应头带 `Deprecation: true` / `Sunset: CP12`，不扣配额、CP12 移除），新代码一律走 `POST /api/v1/articles`。
- 配额用尽 → 429 `code 3001`（弹付费墙）。

### 1.3 状态轮询

`GET /api/v1/articles/{article_id}/status`（ROUTES 已注册）

```json
{
  "article_id": "art_xxx",
  "status": "ready",            // pending | distilling | ready | failed（+ 用户态 listened；Android 强校验枚举，未知值后端已降级为 distilling）
  "task_id": "dst_xxx",         // 蒸馏任务 id（变体接口要用）
  "task_status": "done",        // 内部 7 态原文
  "error": null,
  "audio_url": "https://...",   // 可能为 mock 占位，播放以 §1.4 为准
  "audio_duration_sec": 300,
  "tags": ["科技", "商业"],
  "quality_score": 8.5,
  "created_at": "2026-09-24T09:00:00",
  "updated_at": "2026-09-24T09:00:45"
}
```

- 轮询节奏建议：pending/distilling 阶段 3s 一次；`ready`/`failed` 停止轮询。

### 1.4 取播放地址

`GET /api/v1/articles/{article_id}/audio-url`

- **仅 `status=ready` 可用，否则 404**（404 语义 =「还没好」，客户端继续轮询 §1.3，不要当错误弹）。
- 响应 `AudioUrlResponse`：

```json
{
  "article_id": "art_xxx",
  "audio_url": "http://10.0.2.2:8100/audio/audio/art_xxx.m4a?Expires=...&OSSAccessKeyId=mock&Signature=mock",
  "expires_at": "2026-09-24T10:00:00+00:00",
  "duration_sec": 300
}
```

- ⚠️ `expires_at` 语义：URL 带签名参数（当前 mock，CP1.8+ 真 OSS 签名），**过期后重新请求本端点取新 URL，不要长缓存 URL 本身**。
- 该端点是**主档（128k）**地址。多码率见 §3。

---

## 2. 听感反馈闭环（CP3.7 系列数据入口）

> 后端已按「4 维评分」建好数据表（`distillation_evaluations`），但 **4 维评分的 HTTP 端点尚未开放**（缺口 G1，见 §5）。现阶段 Android 的反馈数据入口仍是下列 1-5 星三端点，它们驱动自动重蒸/画像更新的 hook 链路已在线上跑通。

### 2.1 评分（1-5 星 + 可选评论）

`POST /api/v1/articles/{article_id}/rate`

```json
// 请求
{ "rating": 4, "comment": "结尾稍快" }   // comment 可选；rating ∈ [1,5]
// 响应 200
{ "id": "art_xxx", "rating": 4, "feedback_id": 123 }
```

- 校验失败走业务 400（不是 422）：`rating` 缺失或越界 → `code 4001`。
- 幂等性：**不幂等**（每次调用写一条 feedback），UI 上防连点。

### 2.2 听完上报

`POST /api/v1/articles/{article_id}/listen-complete`

```json
// 请求（可空 body）
{ "duration_sec": 290 }   // 可选：实际收听秒数
// 响应 200
{ "id": "art_xxx", "listened_at": "2026-09-24T09:31:00", "feedback_id": 124 }
```

- 触发条件建议：播放进度 ≥ 90% 或用户手动划走时。ExoPlayer `PLAYBACK_COMPLETE` 事件直连。

### 2.3 跳过（含原因）

`POST /api/v1/articles/{article_id}/skip`

```json
// 请求
{ "reason": "too_long" }   // 必填非空，≤64 字符，自由文本（CP8.6 放宽）
// 响应 200
{ "id": "art_xxx", "skip": true, "feedback_id": 125, "reason": "too_long" }
```

- 推荐枚举（后端用于分类统计，不强制）：`too_long` / `too_short` / `boring` / `low_quality` / `not_interested` / `other`。
- CP3.7.0 的「跳过原因结构化」UI 上线后，reason 仍走本接口，值为 UI 选项的枚举串。

### 2.4 分类反馈（feedback-v2 双轨）

`POST /api/v1/feedback-v2`

```json
// 请求
{
  "article_id": "art_xxx",          // 可选（不传 = 全局反馈）
  "category": "audio_quality",      // 必填: bug|feature|content|audio_quality|other
  "rating": 3,                      // 可选 1-5
  "content": "2 倍速时音频卡顿",      // 必填非空
  "contact": "user@example.com",    // 可选
  "device_info": { "model": "Pixel 8", "api": 34 }  // 可选 dict，建议带
}
// 响应 200
{ "ok": true, "id": 88, "category": "audio_quality" }
```

`GET /api/v1/feedback-v2?category=&limit=50` → `{"feedbacks":[{id, article_id, category, rating, content, created_at}]}`（我的提交列表）。

### 2.5 评分请求推送策略（CP5.6.1，客户端行为约定）

后端策略（触发时机的**数据侧规则**，展示时机由客户端策略库实现）：
- 新用户前 5 篇（fresh/warming 状态）完听后**应当**弹评分引导；5 篇后频率降档。
- 同一用户评分引导 24h 内最多 1 次。
- 客户端本地限流计数器即可（无专用端点——若需服务端下发策略，缺口 G3，见 §5）。

---

## 3. 多码率音频变体（CP7.3.0，本轮新增）

### 3.1 查询可用码率

`GET /api/v1/distill/{task_id}/variants`（task_id = §1.3 状态响应里的 `task_id`，`dst_` 前缀）

```json
{
  "task_id": "dst_xxx",
  "article_id": "art_xxx",
  "variants": [
    { "bitrate": 128, "available": true,  "url": "http://.../audio/art_xxx.m4a?...",  "file_size_bytes": null, "is_main": true },
    { "bitrate": 96,  "available": true,  "url": "http://.../audio/art_xxx.96k.m4a", "file_size_bytes": 720896, "is_main": false },
    { "bitrate": 64,  "available": false, "url": null, "file_size_bytes": null, "is_main": false }
  ]
}
```

- **语义**：128k 是主档（蒸馏产物，恒指向 `audio_url`）；96k/64k 首次请求时服务端按需 ffmpeg 转码生成。
- **首次调用可能慢**（转码 5 分钟音频 ≈ 秒级，超时上限 60s）——不要在主线程调；失败自动降级为该档 `available=false`，接口本身仍 200。
- 重复调用幂等（转码结果入库 `article_audio_variants`，二次命中直接返回）。
- `available=false` 的档位：客户端回退上一档；**全 false 才用主档 URL**。

**客户端码率选择建议（CP7.x 离线优先策略）**：
| 场景 | 建议档位 |
|---|---|
| Wi-Fi 在线播放 | 128k（主档） |
| 蜂窝网络在线播放 | 96k → 回退 128k |
| 通勤预加载（离线缓存） | 64k → 回退 96k |
| 省流量开关打开 | 64k |

### 3.2 预热指定码率（Wi-Fi 预下载前调用）

`POST /api/v1/distill/{task_id}/variants/{bitrate}/warm`（bitrate ∈ {96, 64}）

```json
// 响应 200
{ "task_id": "dst_xxx", "bitrate": 64, "generated": true, "oss_key": "audio/art_xxx.64k.m4a", "file_size_bytes": 481280 }
```

- `generated=false` 表示转码失败/无主音频——**仍是 200**，客户端按「不可预热」处理、走在线播放。
- 非法 bitrate → 400（`bitrate must be one of [96, 64]`）。
- 推荐流程：通勤时段前 Wi-Fi 环境，对「待听列表」top-N 篇依次调 64k warm → 成功（拿 `oss_key`）后拼下载 URL 用 OkDownload 缓存到本地。
  - 下载 URL = §3.1 响应里的 `url` 字段（warm 响应不返 URL，需再调一次 GET variants 拿；两次调用间结果稳定）。

### 3.3 与 audio-url 的关系

- §1.4 `audio-url` = 主档快捷入口（带签名参数，语义不变），**保留兼容，不废弃**。
- §3 = 新增的多档协商。Android 新代码统一走 §3；旧版本走 §1.2 不受影响。

---

## 4. 个性化 / 隐私（CP5.6.0 / CP5.6.1）

**Android 当前无需（也无法）直连**——后端已就绪但 HTTP 端点未开放：

| 能力 | 后端现状 | Android 影响 |
|---|---|---|
| 个性化改写开关（consent opt-in） | `ConsentRecord` 表 + service 已有，**无端点**（G2） | 设置页开关暂灰置 / 隐藏 |
| 个性化生效判定 | A/B（`user_id % 100 < 30`）+ tier + 画像自动路由，蒸馏时无感 | 无感知，听感稿自动不同 |
| 注销删数（GDPR） | `privacy_service.delete_user_data` 已实现，无端点 | 注销流程暂不含「听感画像清理」确认文案（后端补齐前不要写进 UI） |

> 客户端 UI 预留：设置页「个性化听感」开关 + 首次触发弹窗（文案对齐隐私政策 v2），等 G2 端点上线后接 `GET/PUT /api/v1/user/consent`（拟定路径，以最终后端为准）。

---

## 5. 缺口清单（需要后端补端点才能对接的功能）

> 📐 **补齐方案已出**：《补齐方案_接口端点缺口_v1.md》——G1 属批次 B1（P0，最先落地），Android 评分 UI 可按 §5 G1 目标请求体直接开工。

| # | 缺口 | 阻塞的产品功能 | 优先级 | 拟定接口（给后端参考） |
|---|---|---|---|---|
| G1 | **4 维评分提交端点**（hook/section/outro/rhythm + skip_reason） | CP3.7.0 评分 UI → `distillation_evaluations` 表 → 自动重蒸/few-shot 入池闭环拿不到 Android 数据 | 🔴 P0 | `POST /api/v1/distill/{task_id}/evaluation` |
| G2 | consent 读写端点（个性化开关 + 跨用户复用开关） | 设置页个性化开关、GDPR 注销确认 | 🟠 P1 | `GET/PUT /api/v1/user/consent` |
| G3 | 评分引导策略下发（当前冷启动状态 + 是否该弹） | §2.5 推送时机与后端规则同步（现在只能客户端自计次数，口径会漂） | 🟡 P2 | `GET /api/v1/user/cold-start`（返回 state + ratings_remaining） |
| G4 | 月度听感报告（CP5.7.1） | 报告落地页 | 未排期 | notifications deeplink + `GET /api/v1/user/listening-report?month=` |
| G5 | 变体端点 ROUTES 显式注册 | 目前靠 fallback `distill/` 前缀可达，无实际阻塞 | 建议 | config.py 补 2 条 Route（消除 fallback 依赖） |

**Android 侧近期可做的（不阻塞）**：
1. §3 多码率协商 + 64k 预加载（CP7.4.0 的客户端半边，warm 接口已可用）✅
2. §2.1-2.3 反馈三端点接入（评分数据已在驱动画像/重蒸链路）✅
3. G1 的 UI 组件先行开发，端点后补即插（表结构见下）

**G1 目标请求体（与 `distillation_evaluations` 表对齐，UI 现在就可以按这个模型搭）**：
```json
{
  "hook_score": 4,       // 开场吸引力 1-5，可 null
  "section_score": 4,    // 章节节奏 1-5，可 null
  "outro_score": 3,      // 结尾收束 1-5，可 null
  "rhythm_score": 4,     // 语速节拍 1-5，可 null
  "overall_score": 4,    // 总评 1-5，必填
  "comment": "开场很有钩子，结尾收得急",   // 可选
  "skip_reason": null    // 跳过时填结构化原因
}
```

---

## 6. 端到端联调冒烟（curl，模拟器视角）

```bash
GW=http://10.0.2.2:8100
# 1. mock 签发 JWT（联调用；正式走微信登录）
TOKEN=$(curl -s -X POST $GW/api/v1/auth/token -H 'Content-Type: application/json' \
  -d '{"user_id":"1"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# 2. 添加文章（触发蒸馏）
ART=$(curl -s -X POST $GW/api/v1/articles/add -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/article","source":"web"}')
AID=$(echo "$ART" | jq -r .id)

# 3. 轮询到 ready，拿 task_id
TID=$(curl -s "$GW/api/v1/articles/$AID/status" -H "$AUTH" | jq -r .task_id)

# 4. 变体协商（首次会触发按需转码）
curl -s "$GW/api/v1/distill/$TID/variants" -H "$AUTH" | jq

# 5. 预热 64k 档（Wi-Fi 预加载场景）
curl -s -X POST "$GW/api/v1/distill/$TID/variants/64/warm" -H "$AUTH" | jq

# 6. 听完 + 评分
curl -s -X POST "$GW/api/v1/articles/$AID/listen-complete" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"duration_sec":290}' | jq
curl -s -X POST "$GW/api/v1/articles/$AID/rate" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"rating":4,"comment":"不错"}' | jq
```

---

## 7. 变更记录

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.1 | 2026-09-24 | 新增 §3 多码率变体（CP7.3.0）、§4 个性化现状、§5 缺口清单 G1-G5 |
| v1.0 | 2026-09-16 | 初版（CP1.x 主链路） |
