# Stashbox · 听匣（vibecoding MVP）

> **Stashbox = 你的私人听感订阅库** — 把任何地方的好内容（公众号 / 抖音 / PDF / 网页）通过 AI 蒸馏成 5 分钟音频，通勤路上用耳朵深度阅读。

## 项目状态

> 2026-10-06 更新。此前本表停在 CP1.5、CP2–CP8 全部 🔒，与代码严重不符。

| 维度 | 状态 |
|---|---|
| 方案 | v1 r13 / v2 r13（已定版，详见 [`docs/技术方案_v1.md`](./docs/技术方案_v1.md) / [`docs/技术方案_v2.md`](./docs/技术方案_v2.md)）|
| 决策点 | D1-D53，53/53 ✅ |
| 域名 | `stashbox.cn`（个人备案，备案审核中）|
| 当前阶段 | **CP12 附近**（后端最新 cp11.0.8，安卓 main 有 cp12.0.1 接口对齐）|
| 客户端 | iOS + Android 双端设计，**一期只做 Android**（iOS 未启动）|

## Checkpoint 进度

| CP | 名称 | 状态 |
|---|---|---|
| CP1 | 基础架构 + D9 集成 | ✅ |
| CP2 | 收集层 + 多源抓取 | ✅ |
| CP3 | L4 蒸馏引擎 | ✅（真 LLM / TTS 已接，见「技术选型」下方的更正）|
| CP4 | App 端 v1（只 Android）| ✅ 功能基本完整 |
| CP5 | UX 优化 | ✅ |
| CP6 | 内测准备 + D10 评估 | ✅ |
| CP7 | 内测 + 反馈 | ⏳ |
| CP8 | 调优 + 上线 | ⏳ |
| CP9–CP12 | 听感运营 / 数据闭环 / 跨端对齐 | ✅ 进行中 |

**当前真实形态**（与本表早期描述的差异见下方「与早期描述的偏差」）：

- 后端四服务齐全 + PG/Redis + 35 个 alembic 迁移 + 112 个测试文件
- 蒸馏链路已接**真实** LLM 与 TTS（多 provider，可插拔）
- 安卓是单模块 `:app`，111 个 kt 文件 / 16.3k 行 / 16 条路由
- 管理后台 18 个页面，设计系统已重建

## 技术选型的两处更正

| 维度 | 早期描述 | 实际 |
|---|---|---|
| 多模态 LLM | Qwen2.5-VL-72B | **可插拔**，默认 `openai`，另有 `qwen_vl`（`ai-service/config_llm.py`）|
| TTS | 豆包 TTS | **多 provider**：`indextts`（默认，自建 mlx-audio）/ `edge` / `openai` / `doubao` / `local`；mock 仅作兜底 |
| 接口契约 | OpenAPI 3.0 | OpenAPI **3.1**，落盘于 `backend/docs/openapi/stashbox-openapi.json`（改动端点后需重跑 `backend/scripts/export_openapi.py`）|


## 仓库结构（多仓独立，非 monorepo）

> ⚠️ **听匣不是 monorepo**，而是 **3 个独立 git 仓库**，靠 Checkpoint（CP）编号人工对齐，无代码级依赖：
> - `stashbox/`（本仓库）= 服务端
> - `stashbox-android/`（同级）= Android 客户端
> - `stashbox-admin-web/`（同级）= 运营后台

```
stashbox/                      # 仓库 A：服务端（FastAPI 四服务）
├── backend/                  # 四服务 + 共享层（本仓库主体）
│   ├── api-gateway/          # 统一入口 + JWT 鉴权
│   ├── user-service/         # 用户 + 配额 + 登录
│   ├── content-service/      # 文章 + 标签 + 收集
│   ├── ai-service/           # 蒸馏 worker + 队列（Arq）
│   ├── common/               # 共享：DB / 配置 / 日志
│   ├── app/                  # 共享 app 层（services/tts 等）
│   ├── alembic/              # 数据库迁移
│   ├── scripts/              # 运维脚本
│   └── tests/                # pytest
├── docs/                     # 设计文档（技术方案 v1/v2、ADR 预留）
├── infra/                    # 仅本地开发 docker-compose（PG+Redis）；k8s/terraform 未建
├── tests/                    # 仓库级测试
└── Makefile / pyproject.toml # 工程配置

stashbox-android/             # 仓库 B：Android（Kotlin + Compose）
├── app/                      # 当前仅 :app 单模块；core/feature 拆分见 CP4.2→CP4.7
├── gradle/ settings.gradle.kts build.gradle.kts
└── scripts/

stashbox-admin-web/           # 仓库 C：运营后台（React19 + Vite8 + Tailwind3）
```

## 关键技术选型（v1 §0b.1）

| 维度 | 选型 |
|---|---|
| 后端框架 | FastAPI（异步 + 自动 OpenAPI） |
| 数据库 | PostgreSQL（业务数据）+ Redis（缓存/队列）|
| 任务队列 | Arq（Redis 后端，蒸馏 worker，见 `backend/ai-service/arq_settings.py`）|
| 多模态 LLM | 可插拔，默认 `openai`；另有 `qwen_vl`。见上方更正 |
| 听感改写 | 同一套 LLM 工厂 |
| ASR / TTS | 多 provider 可插拔，默认自建 IndexTTS（mlx-audio）。见上方更正 |
| 部署 | 阿里云 ACK + RDS + Redis + OSS + CDN |
| Android | Kotlin + Jetpack Compose + MediaSession |
| 接口契约 | OpenAPI 3.1 + 自动生成 Kotlin SDK |

## 本地测试

```bash
cd backend && source .venv/bin/activate

# 全量（与 CI 同参数；1099 passed / 7 skipped，约 30s）
CI=true STASHBOX_E2E_SKIP_DEVICE=1 pytest tests/ -q

# 单个服务目录
pytest tests/content -q          # 135 passed

# 跨端契约校验（在 ../stashbox-admin-web 下）
cd ../stashbox-admin-web && pnpm contract
```

真机 e2e（`tests/e2e/`）需要设备与**隔离**后端，生产端口会被守卫拒绝执行。

## 快速开始（开发者）

> ⚠️ **生产部署**不部署 Docker / K8s（Hornet 2026-09-16 拍板）。但**本地开发**用 `docker compose` 起 PostgreSQL + Redis 依赖（见下），比装原生 PG 简单。
> 🔌 **默认端口 CP1.5 起改为 8100-8103**（本机 8000/8001/8002 已被其它项目占用）：api-gateway `:8100` / user `:8101` / content `:8102` / ai `:8103`。

```bash
# 1. 安装依赖（Python ≥ 3.11）
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r api-gateway/requirements.txt   # 各服务 requirements.txt 已含全部依赖
# 或逐服务：cd <service> && pip install -r requirements.txt

# 2. 起本地依赖（PostgreSQL + Redis，需本机有 Docker）
cd infra/docker
docker compose -f docker-compose.dev.yml up -d
cd ../../backend && alembic upgrade head       # 建表（users / articles / distilled_articles）

# 3. 一键启动 4 个服务（端口 8100 / 8101 / 8102 / 8103）
bash run_dev.sh          # 内部各起一个 uvicorn 进程，自动设置 PYTHONPATH 与端口

# 4. 或手动各起一个 uvicorn 进程（注意 PYTHONPATH 指向仓库根父目录）
export PYTHONPATH=/Users/hornet/work
cd api-gateway      && uvicorn main:app --reload --port 8100 &
cd user-service     && uvicorn main:app --reload --port 8101 &
cd content-service  && uvicorn main:app --reload --port 8102 &
cd ai-service       && uvicorn main:app --reload --port 8103 &

# 5. 健康检查（四个端口都应返回 {"status":"ok",...}）
curl localhost:8100/health
```

### Observability（Prometheus + Grafana + Alertmanager，CP6.4-pre-2）

4 服务已通过 CP6.4-pre 暴露 `/healthz` `/readyz` `/metrics`，本地用 docker compose 起采集 + 可视化栈：

| 组件 | 地址 | 说明 |
|---|---|---|
| Prometheus | http://localhost:9090 | `/targets` 看 4 服务 scrape 状态，保留 7 天 |
| Grafana | http://localhost:3000 | `admin` / `stashbox_dev`，自动加载 2 个 dashboard |
| Alertmanager | http://localhost:9093 | 告警路由；飞书 webhook 是占位 token（CP1.8 才接真） |

```bash
# ⚠️ 顺序要求：先起 4 服务，再起 observability，否则 /targets 里全是 DOWN
bash backend/run_dev.sh    # 已在跑就不用重启
make obs-up                # 等价于 docker compose -f infra/docker/docker-compose.observability.yml up -d

make obs-logs              # 看日志
make obs-down              # 停容器（prometheus_data / grafana_data volume 保留）
make obs-test              # 离线校验配置：YAML / PromQL / dashboard JSON
```

Prometheus 通过 `host.docker.internal` 抓宿主机端口（8100-8103 + 8104 ai-worker 预留）。
Grafana 数据源和 dashboard 都是 provisioning 自动加载，不需要手工 import：

- `request_overview` —— 4 服务 QPS / 4xx-5xx 错误率 / P50-P95-P99 延迟 / UP 状态
- `distill_pipeline` —— 蒸馏队列长度 + 失败率 + LLM token/成本（后 4 个面板依赖 ai-service 后续埋点，暂时 No data 属正常）

告警规则在 `infra/docker/prometheus/rules/alerts.yml`，8 条，按 v1 §10.5：`HighErrorRate` `HighClientErrorRate` `HighP95Latency` `ServiceDown` `ReadinessCheckFailing` `DistillTaskBacklog` `LLMCostSpike` `RedisConnectionFailing`。
其中 `ReadinessCheckFailing` 依赖 blackbox_exporter、`RedisConnectionFailing` 依赖 redis_exporter，本机未部署，规则先写着（空转不触发）。

> `docker-compose.dev.yml` 已 `include` observability.yml（需 Compose ≥ 2.20）；只要 PG + Redis 的话注释掉那段 include 即可。

环境要求：JDK 17、Android SDK（compileSdk 35 / build-tools 35.0.0）。

```bash
# 1. 配置 SDK 路径（local.properties，已 gitignore，不提交）
#    注意：Android 是独立仓库 stashbox-android/，不是本仓库子目录
cd stashbox-android
echo "sdk.dir=/opt/homebrew/share/android-commandlinetools" > local.properties

# 2. 构建 debug APK（用 Gradle Wrapper，不要系统 gradle）
export JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
export ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
./gradlew assembleDebug          # 产物：app/build/outputs/apk/debug/app-debug.apk

# 3. 静态检查 + 单测
./gradlew lint
./gradlew testDebugUnitTest
```

技术栈：Kotlin 2.0.21 / Gradle 8.10.2 / AGP 8.7.2 / Compose BOM 2024.10.01 / Hilt 2.52 + KSP。
包名 `com.tingxia.audio`（debug 包名加 `.debug` 后缀）。Media3（ExoPlayer + MediaSession）与 Retrofit 已留位，CP4.4 / CP4.6 才接入。

## 接真 LLM / TTS / FFmpeg（CP3.5+）

蒸馏链路默认全 mock（`LLM_PROVIDER=mock` + `TTS_PROVIDER=mock`），可零凭证本地跑通。
切真只需配置 + 凭证，**代码已就绪**。

**统一协议策略（CP9.x 决策）**：LLM 与 TTS 都走 **OpenAI 协议**（HTTP + Bearer auth），便于切换 provider。
- LLM：阿里 token-plan 团队版 OpenAI 兼容端点（`https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions`）
- TTS：火山方舟 ARK OpenAI 兼容端点（`https://ark.cn-beijing.volces.com/api/v3/audio/speech`）
- LLM 与 TTS 的 API key 已拆为 `OPENAI_LLM_API_KEY` / `OPENAI_TTS_API_KEY`，避免双消费方冲突

| 环节 | 实现位置 | 切真方式 | 所需凭证 |
|---|---|---|---|
| Step1 结构化 (Qwen-VL) | `backend/ai-service/llm/qwen_vl.py` | `LLM_PROVIDER=qwen_vl` | `DASHSCOPE_API_KEY`（token-plan API Key） |
| Step2 改写 (OpenAI) | `backend/ai-service/llm/openai.py` | `LLM_PROVIDER=openai` | `OPENAI_LLM_API_KEY` |
| Step3 TTS (Coding Plan 推荐) | `backend/app/services/tts/doubao.py` | `TTS_PROVIDER=doubao` | `DOUBAO_TTS_API_KEY`（Coding Plan 专属 API Key）+ `DOUBAO_TTS_RESOURCE_ID=seed-tts-2.0` + `DOUBAO_TTS_VOICE=<音色库 speaker>` |
| Step3 TTS (OpenAI 协议，仅 OpenAI/Azure) | `backend/app/services/tts/openai.py` | `TTS_PROVIDER=openai` | `OPENAI_TTS_API_KEY`（真 OpenAI sk / Azure key；**不兼容火山 Coding Plan**）|
| Step4 拼接 (FFmpeg) | `backend/ai-service/distill/steps.py::step4_concat` | 自动（Step3 出真实 bytes 时走 ffmpeg）| 本机装 `ffmpeg` + `ffprobe`（本机在 `/opt/homebrew/bin`）|
| 音频存储 (OSS) | `backend/app/services/storage` | 自动（`_save_audio` 上传）| `OSS_*` 凭证（`.env`）|

- LLM 客户端（Qwen-VL / OpenAI）**已是真实 OpenAI 协议 HTTP 客户端**（httpx + 指数退避、SSE 流式），仅默认 mock。**Claude（Anthropic 原生协议）已弃用**——需要 Claude 时走 `LLM_PROVIDER=openai` + 第三方 OpenAI-compatible 代理。
- TTS 推荐 `doubao` provider（Coding Plan）：HTTP POST 单向流式走 `https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional`，鉴权 `X-Api-Key` + `X-Api-Resource-Id: seed-tts-2.0`；请求体 `{user, req_params:{text, speaker, audio_params}}`，响应 JSON `data` 字段 base64 解码为 mp3 bytes。`DOUBAO_TTS_VOICE` 必须从控制台 → 音色库 复制真实可用 speaker ID（例：`zh_female_gaolengyujie_uranus_bigtts`）。
- `OpenAITTSClient` (`TTS_PROVIDER=openai`) 是**真 OpenAI 协议 TTS**（OpenAI 官方 / Azure），**不兼容火山方舟 Coding Plan**（后者走 openspeech.bytedance.com + X-Api-Key，不是 ARK OpenAI 端点）。
- 免费 TTS 备选：`TTS_PROVIDER=edge`（Microsoft Edge TTS，`pip install edge-tts`，无需 key）。
- Step4 拼接已修正：段为 mp3 时改 `-c:a aac` 转码，避免 `-c copy` 把 mp3 塞进 m4a(MP4) 容器报错（`Could not find tag for codec mp3`）；本地用 ffmpeg 实测拼接时长与分段之和一致。
- 完整模板：`backend/.env.example`（已含所有可走方案占位符 + CORS 白名单示例）；真实凭证写 `backend/.env`（已 gitignore）。
- **状态契约**（CP3.5-pre-2）：Android 端 `DistillStatus` 枚举 = `pending | distilling | ready | failed | listened`；后端 ai-service `main.py::_external_distill_status()` 把内部 7 态（`queued / step1-4 / done / failed`）映射到这 5 态，避免 Android 强校验枚举崩溃。

## 许可

本仓库私有（License 待定）。听匣为 Hornet 2026 P1 项目。
