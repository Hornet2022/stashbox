# Stashbox · 听匣（vibecoding MVP）

> **Stashbox = 你的私人听感订阅库** — 把任何地方的好内容（公众号 / 抖音 / PDF / 网页）通过 AI 蒸馏成 5 分钟音频，通勤路上用耳朵深度阅读。

## 项目状态

| 维度 | 状态 |
|---|---|
| 方案 | v1 r13 / v2 r13（已定版，详见 `docs/v1.md` / `docs/v2.md`）|
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
│   ├── docker/          # Docker 镜像
│   ├── k8s/             # ACK 集群 manifest（CP1.1 后）
│   └── terraform/       # 阿里云 IaC（CP1.2 后）
└── docs/                # 文档 + 决策记录
    ├── v1.md            # 技术方案 v1
    ├── v2.md            # 产品方案 v2
    └── decisions/       # ADR（架构决策记录）
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

```bash
# 后端（4 服务 + 依赖）
cd backend
docker-compose -f ../infra/docker/docker-compose.dev.yml up -d
poetry install
cd api-gateway && poetry run uvicorn main:app --reload --port 8000

# Android（待 CP4.2 启动后补全）
cd android
./gradlew assembleDebug
```

## 许可

本仓库私有（License 待定）。听匣为 Hornet 2026 P1 项目。
