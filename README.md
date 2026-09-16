# Stashbox · 听匣（vibecoding MVP）

> **Stashbox = 你的私人听感订阅库** — 把任何地方的好内容（公众号 / 抖音 / PDF / 网页）通过 AI 蒸馏成 5 分钟音频，通勤路上用耳朵深度阅读。

## 项目状态

| 维度 | 状态 |
|---|---|
| 方案 | v1 r13 / v2 r13（已定版，详见 [`docs/技术方案_v1.md`](./docs/技术方案_v1.md) / [`docs/技术方案_v2.md`](./docs/技术方案_v2.md)）|
| 决策点 | D1-D53，53/53 ✅ |
| 域名 | `stashbox.cn`（个人备案，备案审核中）|
| 当前阶段 | **CP1.4 后端骨架**（一人 vibecoding 节奏）|
| 客户端 | iOS + Android 双端设计，**一期只做 Android** |

## Checkpoint 进度

| CP | 名称 | 状态 |
|---|---|---|
| CP1 | 基础架构 + D9 集成 | ⏳ 域名/备案自动化跑 / 后端骨架进行中 |
| CP2 | 收集层 + 多源抓取 | 🔒 |
| CP3 | L4 蒸馏引擎首篇 demo | 🔒 |
| CP4 | App 端 v1（**只 Android**）| 🔒 |
| CP5 | UX 优化 | 🔒 |
| CP6 | 内测准备 + D10 评估 | 🔒 |
| CP7 | 内测 + 反馈 | 🔒 |
| CP8 | 调优 + 上线 | 🔒 |

## 仓库结构（monorepo）

```
stashbox/
├── backend/             # FastAPI 后端（4 服务）
│   ├── api-gateway/     # 统一入口 + JWT 鉴权
│   ├── user-service/    # 用户 + 配额 + 登录
│   ├── content-service/ # 文章 + 标签 + 收集
│   ├── ai-service/      # 蒸馏 worker + 队列
│   ├── common/          # 共享：DB / 配置 / 日志
│   └── tests/           # pytest
├── android/             # Android Kotlin
│   ├── app/             # Application module
│   ├── core/            # 网络/数据/播放器（横切关注）
│   └── feature/         # 列表/详情/播放器/登录
├── api-spec/            # OpenAPI 3.0 接口契约
│   ├── openapi.yaml
│   └── generated/       # 自动生成的客户端 SDK
├── infra/               # 部署
│   ├── docker/          # 暂不启用：本仓库不部署 Docker（Hornet 拍板）
│   ├── k8s/             # ACK 集群 manifest（CP1.1 后）
│   └── terraform/       # 阿里云 IaC（CP1.2 后）
└── docs/                # 设计文档
    ├── README.md        # 设计文档总入口
    ├── 技术方案_v1.md    # 技术视角（CP1-CP8）
    ├── 技术方案_v2.md    # 产品视角
    └── decisions/       # ADR（架构决策记录，未来用）
```

## 关键技术选型（v1 §0b.1）

| 维度 | 选型 |
|---|---|
| 后端框架 | FastAPI（异步 + 自动 OpenAPI） |
| 数据库 | PostgreSQL（业务数据）+ Redis（缓存/队列）|
| 任务队列 | Celery / Dramatiq（蒸馏 worker）|
| 多模态 LLM | Qwen2.5-VL-72B（多模态理解）|
| 听感改写 | Claude 4 Sonnet |
| ASR / TTS | 豆包 ASR + 豆包 TTS |
| 部署 | 阿里云 ACK + RDS + Redis + OSS + CDN |
| Android | Kotlin + Jetpack Compose + MediaSession |
| 接口契约 | OpenAPI 3.0 + 自动生成 Kotlin SDK |

## 快速开始（开发者）

> ⚠️ 本仓库**不部署 Docker / K8s**（Hornet 2026-09-16 拍板）：本地直接 `uvicorn` 启动各服务，容器化方案留到 CP1.x 末段单独任务包再定。

```bash
# 1. 安装依赖（Python ≥ 3.11）
cd backend
python -m venv .venv && source .venv/bin/activate
poetry install
# 各服务也可独立安装：cd <service> && pip install -r requirements.txt（CP1.4 起）

# 2. 一键启动 4 个服务（api-gateway 8000 / user 8001 / content 8002 / ai 8003）
bash run_dev.sh          # CP1.4 提供，内部各起一个 uvicorn 进程

# 3. 或手动各起一个 uvicorn 进程
cd api-gateway      && uvicorn main:app --reload --port 8000 &
cd user-service     && uvicorn main:app --reload --port 8001 &
cd content-service  && uvicorn main:app --reload --port 8002 &
cd ai-service       && uvicorn main:app --reload --port 8003 &

# 4. 健康检查（四个端口都应返回 {"status":"ok",...}）
curl localhost:8000/health
```

```bash
# Android（待 CP4.2 启动后补全）
cd android
./gradlew assembleDebug
```

## 许可

本仓库私有（License 待定）。听匣为 Hornet 2026 P1 项目。
