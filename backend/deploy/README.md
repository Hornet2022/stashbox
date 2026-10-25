# Stashbox 后端 —— 部署产物

容器化 + 生产的编排、环境变量模板、预检脚本。

```
backend/
├── Dockerfile                          一个文件，6 个 target（见下）
├── .dockerignore                       build context 排除表（含 .env）
└── deploy/
    ├── docker-compose.production.yml   生产编排（外部 RDS / Redis）
    ├── docker-compose.production.local.yml  本地叠加文件（自带 PG / Redis）
    ├── .env.production.example         环境变量模板（无任何真实凭证）
    ├── .env.production.2g.example      环境变量模板（**2 GiB 轻量服务器**专用，第 9 节）
    ├── preflight.sh                    部署前预检（FAIL 就别发）
    ├── deploy.sh                       预检 → 构建 → 迁移 → 启动
    ├── tune-host.sh                    2 GiB 机器的主机调优（用户/PG/Redis/swap/journald/sysctl）
    ├── systemd/                        2 GiB 机器的 systemd 部署路径（6 个单元 + nginx.conf）
    └── README.md                       本文件
```

开发路径**不受影响**：`backend/run_dev.sh` 和 `backend/scripts/*` 一行没动，
它们仍然是本机 macOS launchd 的开发入口。

---

## 0. 本地与云端的分工（硬约束）

| | 本地工程 | 云服务器工程 |
|---|---|---|
| 角色 | **测试环境** | **生产环境** |
| 代码身份 | **唯一真相源** | **部署产物**（从本地同步过去） |
| 改代码 | ✅ 唯一允许的地方 | ❌ 禁止 |

### 三条铁律

1. **问题修复一定在本地执行**：改代码 → 跑测试 → 提交 → 同步到云端部署。
   云端跑的是**部署产物**，不是工作区。
2. **问题排查可以直接看云端的生产日志** —— 只读地看 journalctl / nginx 日志、
   跑只读的 `psql -c "select …"`、看服务状态、curl 一下线上接口，都允许，
   因为它们不改任何东西。
3. **不要直接在云服务器上改工程代码。**

### 什么算「改代码」，什么不算

| 允许 | 禁止 |
|---|---|
| 本地改源码 → 跑测试 → 提交 → rsync/deploy 到云端 | 在云端用 vim / sed 改 `.py` `.kt` `.ts` 等源码 |
| 在云端跑**仓库里的**部署脚本（`patch-nginx-*.py`、`renew-tls.sh`、`switch-domain.sh`…） | 手改 `/etc/nginx/sites-available/*`（那是 patch 脚本的产物，要改就改仓库里的脚本再跑） |
| 改云端 `/etc/stashbox/stashbox.env`（**服务器专属配置**，含凭证、不在仓库里） | 在云端 `git commit`（仓库在本地，提交要能被评审和回滚） |
| 只读地看日志 / 查库 / 看状态 / curl 线上 | 把云端当工作区：`python -c` 就地改文件、临时打补丁 |

### 为什么

- **云端手改的代码活不过下一次部署**：部署是「本地 → 云端」的同步，云端手改的
  部分要么被覆盖、要么和本地版本分叉，而本地没有这段代码 → 仓库与生产环境
  漂移，之后谁也说不清线上跑的是哪一版。
- **生产环境的改动不可复现、不可回滚、不可评审**：没有 commit 就没有 diff，
  出事时既不知道改了什么，也退不回去。
- **在云端改就丢掉了 CI**：本地改至少会跑一遍全量测试（当前 1355 passed /
  9 skipped）。

### 发现云端有本地没有的改动怎么办

**拉回本地**，审一遍、跑测试、提交，再同步回去 —— 而不是继续在云端改。

> 2026-10-22 实测踩过：本地改了、部署了、但忘了 commit，服务器与仓库就漂移了；
> 下一次从仓库部署，会把线上已经修好的东西覆盖回去（那次修的是「同一份音频
> 落盘两遍」和「nginx 把音频缓冲到临时文件」）。

### 同步代码到云端的标准做法

```bash
# 改完本地 → 跑测试 → 提交 → 同步
cd backend && .venv/bin/python -m pytest tests/ -q
git commit -am "fix(xxx): …"
rsync -a <改动的文件> root@<服务器>:/opt/stashbox/stashbox/backend/<路径>
# 改了依赖或 service 定义 → 同步完要重启对应服务
ssh root@<服务器> "systemctl restart stashbox-<service>"
```

---

## 1. 镜像设计

### 1.1 基础镜像

`python:3.13-slim-bookworm`

- **3.13**：`backend/.venv` 里实际验证过的解释器是 3.13.12，`pyproject.toml`
  声明 `python = "^3.11"`（3.13 在范围内），CI 用 3.11。
- **slim**：基础层约 150MB，不是 full 的 1GB+。
- **bookworm**：Debian 12，ffmpeg / playwright 的系统依赖都有现成包。

> ⚠️ 这是**小版本滚动 tag**，上游每次 patch 重建都会变内容。正式发版请改成
> 精确 patch 甚至 digest：`python:3.13.12-slim-bookworm@sha256:<digest>`。
> 本机无法访问 `registry-1.docker.io` / `auth.docker.io`，取不到真实 digest，
> 所以这里**没有**写一个编造的 digest —— 编造的 digest 比滚动 tag 更危险。

### 1.2 一个 Dockerfile，6 个 target

| target | 内容 | 谁用 |
|---|---|---|
| `builder` | 按 `poetry.lock` 装依赖到 `/opt/venv` | 只有构建期 |
| `runtime` | ffmpeg + 非 root 用户 + 源码 | 基础 |
| `content` | `runtime` + playwright chromium | content-service |
| `api-gateway` / `user-service` / `ai-service` | `runtime` + 各自的端口与 CMD | 三个服务 |
| `content-service` | `content` + 8102 | content-service |
| `worker` | `runtime` + `python -m arq worker.WorkerSettings` | Arq worker |
| `migrate` | `runtime` + `alembic upgrade head` | 一次性迁移 |

**为什么一个镜像而不是四个**：4 个服务的依赖集合几乎完全相同
（fastapi / uvicorn / sqlalchemy / asyncpg / redis / jose / passlib / loguru /
structlog / prometheus-client），真正的差异只有两处 —— content-service 额外要
playwright chromium（~150MB），ai-service 额外要 langgraph / langfuse / arq / mcp。
拆成 4 个镜像的代价是 4× 构建时间、4× 仓库存储、4 份依赖漂移风险，省下的体积
只有 5% 左右（大头 sqlalchemy / langgraph 大家都装）。所以选"共享 runtime + 1 个
content 变体"。将来某个服务依赖明显分叉时再单独拆。

**依赖怎么钉**：镜像里跑 `poetry install --only main --no-root`，跟 CI
（`.github/workflows/test.yml`）同一套解析结果，版本来自 `poetry.lock`。
因此**没有** `requirements-docker.txt`（项目用 poetry，不用 pip）。
poetry 版本钉在 `2.5.1`（≥ 2.3.4，修掉了 2.1.x 的 wheel/sdist 路径穿越
CVE-2026-34591 / CVE-2026-41140，别降级）。

### 1.3 几个容易踩的细节

- **PYTHONPATH=/app**：代码里全是 `stashbox.backend.common.config` 这种绝对导入，
  `stashbox` 是**仓库根目录**这个包，所以仓库根的**父目录**必须在 sys.path 上
  （`run_dev.sh` 就是 `export PYTHONPATH="$REPO_PARENT:..."`）。
  build context 是 `backend/`，根目录的 `__init__.py` 在 context 之外，
  所以镜像里把布局还原成 `/app/stashbox/backend/...` + 重建 `/app/stashbox/__init__.py`。
- **各服务的工作目录 = 自己的目录**：`python -m uvicorn main:app` 要从 CWD 找
  `main.py`，`from config import ...` / `from arq_settings import ...` /
  `from observability.metrics import ...` 这些顶层模块导入也依赖 CWD 在 sys.path 上。
  alembic 必须 CWD=`backend/`（`alembic.ini` 里 `script_location = alembic` 是相对 CWD 的）。
- **ffmpeg 是硬依赖**：`_find_bin` 探测 `PATH` 和 `/usr/bin` 等位置，
  `transcode_bytes` 真的 fork 它做多码率 m4a 转码。镜像里装 Debian 的 ffmpeg，
  并把 `FFMPEG_BIN` 指到 `/usr/bin/ffmpeg`（代码默认值是 macOS 的 `/opt/homebrew/bin/ffmpeg`）。
- **非 root**：uid/gid 固定 10001，`/var/audio` 和 ai-service 的 `data/audio` 可写。

---

## 2. 数据库 / Redis / 对象存储怎么配

### 2.1 ⚠️ 本项目不读 `DATABASE_URL`

这是最容易配错的一点。`common/config.py` 的 `Settings.database_url` 是**现场拼**出来的：

```
postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}
```

配 `DATABASE_URL` 会被**静默忽略** —— 应用转去连 `localhost` 然后失败。
要接托管 RDS，配的是 `POSTGRES_*` 这一组。

Redis 同理：应用侧读 `REDIS_HOST` / `_PORT` / `_DB` / `_PASSWORD`；
**只有 Arq worker**（`ai-service/arq_settings.py`）另外直接读 `REDIS_URL`。
两个必须指向同一个实例，否则 worker 起着、不报错、但任务永远没人消费。
`preflight.sh` 会逐项比对它们是否一致。

### 2.2 生产用托管实例

| 东西 | 生产 | 本地叠加文件 |
|---|---|---|
| PostgreSQL | 阿里云 RDS（填 `POSTGRES_*`） | `postgres:15-alpine` 容器 |
| Redis | 阿里云 ApsaraDB Redis（填 `REDIS_*` + `REDIS_URL`） | `redis:7-alpine` 容器 |
| 对象存储 | 阿里云 OSS，走 S3 兼容端点（`OSS_*`） | 同样用 OSS，或 `STORAGE_PROVIDER=local` |

OSS 变量：`OSS_ENDPOINT`（如 `oss-cn-hangzhou.aliyuncs.com`）、`OSS_BUCKET`、
`OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`OSS_PUBLIC_BASE_URL`（CDN 域名，可选）、
`OSS_REGION`（**必须与 bucket 地域一致**，代码默认 `us-east-1` 对阿里云是错的）。

---

## 3. 本地跑通

需要 Docker（含 compose v2 ≥ 2.24 —— `env_file` 的 `required: false` 从这版才有）。

```bash
cd backend/deploy
cp .env.production.example .env.production
# 至少要填：JWT_SECRET / ADMIN_BOOTSTRAP_PASSWORD / STASHBOX_CORS_ALLOW_ORIGINS
#           / TRUSTED_PROXY_IPS / PUBLIC_GATEWAY_URL
./preflight.sh          # 先看缺什么
./deploy.sh --local     # 预检 → 构建 → 迁移 → 启动
```

`--local` = 生产编排 + 本地叠加文件（自带 PG / Redis 容器与数据卷）：

```bash
docker compose --env-file .env.production \
  -f docker-compose.production.yml \
  -f docker-compose.production.local.yml up -d
```

起来之后：

```bash
curl http://localhost:8100/healthz     # 网关存活
curl http://localhost:8100/readyz      # 网关就绪（探 PG + Redis）
docker compose ... ps                  # 看健康状态
```

> 本机（macOS）还有一条老路：`backend/run_dev.sh`（裸 uvicorn + launchd）。
> 那是开发路径，**没有**容器隔离，也没有本目录里的这些门禁。

---

## 4. 生产部署（自带 Docker 的机器）

```bash
cd backend/deploy
cp .env.production.example .env.production   # 填真值
./preflight.sh                                # 必须 0 FAIL
./deploy.sh                                   # 预检 → 构建 → 迁移 → 启动
```

`deploy.sh` 做的事：预检 → `docker compose build`（一次 build，多 target，
BuildKit 复用公共层缓存）→ 跑一次 `migrate` → `up -d` → 打印状态。

单独执行某一步：

```bash
./deploy.sh --build-only      # 只构建
./deploy.sh --migrate-only    # 只跑迁移
./deploy.sh down              # 停栈（不删数据卷）
./deploy.sh --skip-preflight  # 跳过预检（不建议）
```

### 启动顺序

```
migrate（alembic upgrade head，跑完退出）
  └─→ user-service / content-service / ai-service / worker
       └─→ api-gateway（等 3 个下游都 healthy 才起）
```

迁移用 `condition: service_completed_successfully` 卡死：迁移失败 = 整个栈起不来，
不会出现"服务先起、表还没建"的中间态。

### 网络

- `internal`（`internal: true`）：服务之间东西向流量，**没有对外路由**，
  外部无法直连 8101/8102/8103。
- `egress`（普通 bridge）：出网流量（LLM / TTS / OSS / 厂商推送）。
  4 个服务都要出网，所以每个容器同时挂两张网。
- **只有 api-gateway 发布端口**（`8100:8100`）。

---

## 5. 部署到阿里云 ACK

compose 文件不能直接跑在 ACK 上（ACK 是 K8s，不是 compose）。做法：

1. **构建镜像并推到 ACR**（阿里云容器镜像服务）：

   ```bash
   cd backend
   docker build -f Dockerfile --target api-gateway -t <registry>/stashbox-backend:2026-10-21 .
   docker build -f Dockerfile --target content-service -t <registry>/stashbox-backend-content:2026-10-21 .
   docker build -f Dockerfile --target worker       -t <registry>/stashbox-backend-worker:2026-10-21 .
   docker build -f Dockerfile --target migrate      -t <registry>/stashbox-backend-migrate:2026-10-21 .
   docker push <registry>/stashbox-backend:2026-10-21
   ```

   （4 个服务共用 `runtime` 层，只有 content 多一层浏览器。）

2. **迁移做成一个 Job**（或 Deployment，跑完即退出），在所有服务之前：

   ```yaml
   # Job 的容器：command: ["alembic", "upgrade", "head"]
   # workingDir: /app/stashbox/backend
   ```

   ACK 里用 `helm` / `kubectl apply` 之前，先跑一次：
   `kubectl create job --from=cronjob/stashbox-migrate stashbox-migrate-<日期>`

3. **4 个服务各一个 Deployment** + 1 个 worker Deployment：
   - `env` 来自 Secret（`JWT_SECRET`、`POSTGRES_PASSWORD`、`REDIS_PASSWORD`、
     `OSS_ACCESS_KEY_SECRET`、各 API key）+ ConfigMap（其余）。
   - `readinessProbe` → `/readyz`；`livenessProbe` → `/healthz`。
   - `securityContext.runAsUser: 10001`（与镜像里的用户一致）。
   - `envFrom` 用 `.env.production` 的内容（K8s 没有 env_file 语法，需要转成 Secret/ConfigMap）。

4. **网关做 Service + Ingress**：`Service` type=ClusterIP，Ingress 指向它，
   只暴露网关的 8100。

5. **`TRUSTED_PROXY_IPS` 填 SLB / Ingress 的网段**（例 `10.0.0.0/8`）。
   这决定网关采信谁的 `X-Forwarded-For` —— 下游的登录限流按真实客户端 IP 计数。
   填 `*` 等于让公网客户端伪造来源 IP 绕过限流。

6. **上线前跑一次预检**：把 ACK 里生效的那份环境变量导出成一份 env 文件
   （K8s Secret → 文件），然后：

   ```bash
   ./preflight.sh /path/to/rendered-prod.env
   ```

   这一步能提前抓到"Secret 忘了注入"、"CORS 没配"、"OSS key 是上个月的"这类问题。

---

## 6. 预检脚本

```bash
./preflight.sh                       # 读同目录的 .env.production
./preflight.sh /path/to/.env.prod    # 指定文件
SKIP_NETWORK=1 ./preflight.sh        # 跳过 DB/Redis 连通性（离线校验）
```

输出是一张 PASS / WARN / FAIL / SKIP 表，**任何一项 FAIL 就 exit 1**。
`deploy.sh` 会自动先跑它，FAIL 就中止部署。

它检查的东西（括号里是它拦的是什么事故）：

| 检查 | 拦的是什么 |
|---|---|
| `ENVIRONMENT=prod`、`DEBUG=false` | 跑在 dev 模式上；SQL 全量进日志 |
| `JWT_SECRET` 存在 / 非 dev 默认值 / ≥16 字符 | 任何人可伪造 token |
| `ADMIN_BOOTSTRAP_PASSWORD` 存在且非 dev 默认值 | 迁移 0015 会用**公开的**密码 `admin@stashbox123` 给 admin 账号 |
| `STASHBOX_ALLOW_DEV_JWT` / `STASHBOX_ALLOW_DEV_TOKEN` **没配** | 两个生产后门开关（连写 `0` 都会生效） |
| `STASHBOX_CORS_ALLOW_ORIGINS` 存在且不是 `*` | 网关 prod 下拒绝启动 / 后台 cookie 暴露给任意站点 |
| `TRUSTED_PROXY_IPS` 存在且不是 `*` | 限流可被伪造来源 IP 绕过 |
| `PUBLIC_GATEWAY_URL` 存在 | 客户端拿到内网 / localhost 地址 |
| `POSTGRES_HOST/USER/PASSWORD/DB` 存在且非占位符 | 连错库 |
| PostgreSQL 可连通（有 `psql` 时连认证+查询一起验） | 白名单 / 密码错 |
| `REDIS_HOST` / `REDIS_PASSWORD` 存在 | Redis 无认证 = VPC 内任意 Pod 可读写 |
| `REDIS_URL` 与 `REDIS_*` 逐项一致 | worker 和 web 连到不同实例 → 任务没人消费 |
| Redis 可连通（有 `redis-cli` 时验 PING） | 队列不通 |
| `STORAGE_PROVIDER=oss` + `OSS_*` 四项齐备 | 音频传不上去 / 多码率转码静默失败 |
| `STORAGE_PROVIDER=local` 时必须显式确认 | 本地盘当生产存储 |
| `OSS_REGION` 已设 | 签名 region 与 bucket 地域不符 → 上传失败 |
| 推送通道已配置（或显式 `STASHBOX_ALLOW_NO_PUSH=1`） | 通知全部标 failed |
| 应用版本号已设（且 `STASHBOX_APP_DOWNLOAD_URL` 同时有值） | 客户端提示更新却没地方下载 |
| `LLM_PROVIDER` / `TTS_PROVIDER` 不是 `mock`；`local` 直接 FAIL | 不产内容 / macOS `say` 在 Linux 不可用 |
| 网关 → 下游 URL 不是旧端口 8001/8002/8003 | 全量 502 |
| ffmpeg 存在（本机或镜像内） | 音频拼接与转码断掉 |
| 数据库 `alembic_version` == 仓库 head（`0038`） | 表结构落后于代码 |
| compose 文件能解析 | 编排本身写错 |

脚本只读，不改任何东西（不改 `.env`、不改数据库、不重启服务）。
解析 env 文件用 `awk` 而不是 `source` —— env 文件是配置，不是 shell 脚本。

---

## 7. 已知限制 / 需要决策的事

- **镜像 digest 没钉死**：`python:3.13-slim-bookworm` 是滚动 tag。本机连不上
  Docker Hub，取不到 digest。发版前建议手工换成精确 patch tag。
- **playwright chromium 只在 `content` target 里**。`content-service` 有无头浏览器
  通道（剪藏 Tier 2 兜底），其余 3 个服务没有 —— 需要的话自己加 target。
- **`infra/mlx-audio-tts-service` 是 macOS 专属**，没有进镜像。
  生产 TTS 用外部 provider（`TTS_PROVIDER=openai` 走火山方舟 ARK / `doubao`），
  或把 IndexTTS 单独部署一套并改 `INDEXTTS_BASE_URL`。
  `TTS_PROVIDER=indextts` 在容器里只有在你另外部署了 TTS 端点时才成立。
- **`ADMIN_BOOTSTRAP_PASSWORD` 只在首次迁移生效**（0015 只在 email/password_hash
  为 NULL 时写入）。之后改密码要走管理后台，改环境变量不会重新触发。
- **`INDEXTTS_REF_AUDIO` 没有默认值**：镜像里不带参考音频（`data/` 已被
  `.dockerignore` 排除，因为那是测试夹具）。用 indextts 的话要自己挂载。
- **`TZ=Asia/Shanghai` 必须与 RDS 时区一致**，否则应用本地时间与数据库 `NOW()` 对不上。
- **`.env.production` 已被仓库根 `.gitignore` 排除**，但填完还是自己确认一次
  `git status` 里没有它。
- **本机没有 Docker**（`docker: command not found`），所以这些产物**没有**做过
  真实镜像构建。已做的静态验证见交付报告；构建与端到端启动需要在有 Docker 的机器上跑一遍。

---

## 8. 与既有资产的关系

- `backend/run_dev.sh`、`backend/scripts/*`：**没动**（macOS launchd 开发路径）。
- `backend/alembic/**`、`backend/**/main.py`：**没动**。
- `infra/docker/docker-compose.dev.yml`：开发用的 PG/Redis，没动；
  本目录的 `docker-compose.production.local.yml` 是生产编排的叠加文件，与它互不干扰。
- `infra/docker/docker-compose.observability.yml`（Prometheus / Grafana / Alertmanager）：
  没动。生产环境里给网关加 `ServiceMonitor` 抓 `/metrics` 即可（`common/observability.py`
  已经把 `/metrics` 挂在所有服务上）。

---

## 9. 2 GiB 机器（轻量应用服务器）怎么部

这条路径给的是**固定规格**的阿里云轻量应用服务器：

```
2 vCPU / 2 GiB RAM / 40 GiB 系统盘 / 200 Mbps 峰值 / 1 IPv4 / Ubuntu 24.04 / 地域 武汉
```

它和上面第 4 节的 compose 路径是**两条独立的部署路径**：
compose 路径适合有 Docker 的大机器；这条路径**不用 Docker**，
5 个 Python 进程直接跑在 systemd 下。

### 9.1 这条路径长什么样

```
                    公网（只放行 22/80/443）
                              │
                    nginx :443  ← TLS 终止（安卓 release 版拒绝明文 HTTP）
                              │
        ┌─────────────────────┴──────────────────────┐
        │  127.0.0.1:8100   api-gateway              │  ← 只有 nginx 能碰
        │  127.0.0.1:8101   user-service             │
        │  127.0.0.1:8102   content-service          │
        │  127.0.0.1:8103   ai-service               │
        │  （无端口）        ai-worker (arq)          │
        └────────────────────────────────────────────┘
        本机：PostgreSQL:5432 / Redis:6379 / journald
        外部：阿里云 OSS（音频）、LLM API、edge-tts
```

**8100-8103 必须只监听 127.0.0.1，公网防火墙不得放行。**
8100 是内网 API 端口：暴露它等于让任何人绕过 TLS 终止直接打 API，
还能自己伪造 `X-Forwarded-For` 绕过登录限流
（见 `backend/common/ip.py` 的模块文档）。5432 / 6379 同理。

### 9.2 步骤

```bash
# ① 机器：Ubuntu 24.04 轻量应用服务器（2 vCPU / 2 GiB / 40 GiB）
#    控制台防火墙只放行 22 / 80 / 443 —— 8100-8103、5432、6379 一个都别放。

# ② 装依赖 + 调优机器（创建服务用户、PG/Redis 调优、swap、journald 限容、sysctl）
cd backend/deploy
sudo bash tune-host.sh --install-deps
#    想先看它要干什么：sudo bash tune-host.sh --dry-run
#    ⚠️ tune-host.sh 从 env 文件里读 REDIS_PASSWORD 写进 redis.conf 的
#       requirepass —— 所以**先填 env 文件再跑它**（或跑两遍）。

# ③ 放代码 + 建 venv
sudo mkdir -p /opt/stashbox && sudo chown stashbox:stashbox /opt/stashbox
sudo -u stashbox git clone <repo> /opt/stashbox/stashbox
sudo -u stashbox python3 -m venv /opt/stashbox/venv
#
# ⚠️ **先配阿里云内网源**，否则 pip 会超时：
#    默认 pypi.org 在这台机器上实测 10s 读超时（装 edge-tts 时直接
#    ReadTimeoutError 崩掉）；mirrors.cloud.aliyuncs.com 实测 0.086s。
#    tune-host.sh 已经配好了（跑一次即可），这里只列手工等价命令：
#     echo '[global]
#     index-url = https://mirrors.cloud.aliyuncs.com/pypi/simple/
#     trusted-host = mirrors.cloud.aliyuncs.com
#     timeout = 60' > /etc/pip.conf
#
sudo -u stashbox /opt/stashbox/venv/bin/pip install -U pip
cd /opt/stashbox/stashbox/backend
#
# ⚠️ **用 pip + 冻结清单装**（服务器上实测唯一可靠的路径）：
#     sudo -u stashbox /opt/stashbox/venv/bin/pip install --no-cache-dir --no-deps \
#          -r /opt/stashbox/requirements-deploy.txt
#
#    ⚠️ **--no-deps 是必须的**，别去掉：
#       清单里已经包含了**全部传递依赖**（pip freeze 的产物），
#       不需要 pip 重新求解。而一旦让 pip 求解，它会因为清单里
#       「看起来矛盾」的 opentelemetry 组合而报 ResolutionImpossible：
#         mcp 2.3.0              → opentelemetry-api >=1.28.0
#         opentelemetry-sdk 1.44.0 → opentelemetry-api ==1.44.0
#         清单里的 opentelemetry-api 是 1.45.1
#       这不是清单错了 —— 开发机的 .venv 里就是这个组合
#       （pip freeze 如实记录了实际安装状态），只是 pip 的求解器
#       比 pip freeze 更挑剔。--no-deps 直接按清单落盘，正是我们要的。
#
#    清单从开发机导出，与本机 .venv / poetry.lock 完全一致：
#     cd backend && .venv/bin/pip list --format=freeze \
#       | grep -viE "^-e|^#" | grep -v "^pkg-resources|^setuptools" \
#       | sort > requirements-deploy.txt
#
#    ❌ **不要**用 `pip install -r requirements.txt`：
#       顶层 requirements.txt 只装了 OpenAPI schema 校验器（dev-tools），
#       **不含**运行时依赖 —— 尤其缺 edge-tts（TTS）和 boto3（OSS 存储），
#       这两个是 app/services/tts/edge.py 和 app/services/storage/oss.py 直接 import 的。
#       按它装完，服务起来会在第一次 TTS / 第一次上传 OSS 时才炸。
#       （各服务目录下的 <service>/requirements.txt 同样不含这两个，别用。）
#
#    ❌ **不要**在这台机器上用 `poetry install`：
#       poetry 的 source 优先级由 pyproject.toml 里的 [[tool.poetry.source]]
#       决定，光写 ~/.config/pypoetry/config.toml 不生效 —— 实测它照样去
#       files.pythonhosted.org 拉包，然后超时。pip + /etc/pip.conf 没这个问题。
#
#    装完自检（三个必须都能 import，否则别往下走）：
#    sudo -u stashbox /opt/stashbox/venv/bin/python -c \
#      "import edge_tts, boto3, arq, fastapi, sqlalchemy, redis; print('deps OK')"

# ④ 环境变量（**2 GiB 专用模板**，不是 .env.production.example）
#
# ⚠️ **从模板复制，不要手写**。手写过一次，漏了 39 项配置，
#    其中 `STASHBOX_FETCH_BROWSER_TIER=0` 漏掉的后果是：
#    每次剪藏都在「浏览器通道不可用」上白等 30 秒抓取预算，
#    45 秒超时且文章不入库 —— 而模板里明明写着这一行。
#    其它漏掉的还有 ARQ_MAX_JOBS=2（2G 机器的并发上限）、
#    ENABLE_MCP_TOOLS=0 等 —— 漏一个都可能表现成「服务看起来正常但很慢」。
sudo install -m 0640 -o root -g stashbox \
     backend/deploy/.env.production.2g.example /etc/stashbox/stashbox.env
sudo vi /etc/stashbox/stashbox.env    # 填 JWT_SECRET / 密码 / OSS key
sudo bash backend/deploy/tune-host.sh   # 再跑一次：把 REDIS_PASSWORD 写进 redis.conf
#
# ⚠️ **改完 env 必须重启受影响的进程** —— systemd 的 EnvironmentFile
#    是**启动时**读一次的，改文件不会热更新：
#      sudo systemctl restart stashbox-user-service.service   # 改 app/version 等
#    （LLM / TTS 的 key 例外：它们走 DB，后台改完立即生效，不用重启。）

# ⑤ 建库建号（本机 PostgreSQL）
sudo -u postgres psql -c "CREATE ROLE stashbox LOGIN PASSWORD '<你填进 env 的密码>'"
sudo -u postgres createdb -O stashbox stashbox

# ⑥ systemd 单元
sudo cp backend/deploy/systemd/stashbox-*.service /etc/systemd/system/
sudo cp backend/deploy/systemd/run-service.sh \
     /opt/stashbox/stashbox/backend/deploy/systemd/run-service.sh
sudo chmod 0755 /opt/stashbox/stashbox/backend/deploy/systemd/run-service.sh
sudo systemctl daemon-reload

# ⑦ 迁移（**先迁移，后起服务** —— 迁移失败就别往下走）
sudo systemctl start stashbox-migrate.service
sudo systemctl status stashbox-migrate.service

# ⑧ 起 5 个服务
sudo systemctl enable --now stashbox-api-gateway.service \
     stashbox-user-service.service stashbox-content-service.service \
     stashbox-ai-service.service stashbox-ai-worker.service

# ⑨ nginx + 证书
#
#    ⚠️ 用 install-nginx-assets.sh，**不要**只 cp nginx.conf。
#    站点配置 include 了两个共享片段（stashbox-downloads.conf /
#    stashbox-downloads-redirect.conf），nginx 对 include 一个不存在的
#    文件是 **emerg**（不是 warning）→ `nginx -t` 直接挂 →
#    站点起不来。2026-10-22 走查发现：仓库里没有任何脚本会拷
#    stashbox-downloads.conf（只有 patch-nginx-downloads-redirect.py
#    拷了另一个）→ 严格照步骤部署的新机器在第 ⑨ 步就死。
# ⑨-补 备案通过后的切换（2026-10-23）
#
#    ICP 备案已通过 → 「备案未过期的临时措施」全部撤掉。三处联动：
#
#    ① 域名入口的 /downloads/ ：302 到 IP  →  **直出**
#       备案前：未备案域名的流量被云厂商限制（大流量被掐），所以 302 到 IP 绕开。
#       备案后：限制解除，302 变成多余的一跳（跨主机跳转，对下载器不友好），
#               而且营销页链接的语义变成「域名只是个跳转器」。
#
#       python3 backend/deploy/patch-nginx-downloads-redirect.py --direct
#       # 回退（万一云厂商策略又变、或域名线路出问题）：
#       python3 backend/deploy/patch-nginx-downloads-redirect.py
#
#       脚本是**双向**的，且自带备份 + `nginx -t` 失败自动回滚。
#       IP 入口（server_name _）始终直出（不能跳自己）。
#
#    ② 服务端下发的下载地址：IP → 域名
#       STASHBOX_APP_DOWNLOAD_URL=https://stashbox.cn/downloads/tingxia-<ver>.apk
#       （release_apk.sh 现在自动做这件事，别手改 env）
#
#    ③ 营销页：主按钮本来就是相对路径 /downloads/latest.apk，**不用改** ——
#       切直出后它自动走域名。
#       ⚠️ 但「走 IP 直连下载」那条**第二层退路要留着**（切直出后
#       域名与 IP 重新变成两条不同的路，它又变得有意义了）。
#       2026-10-23 中间态曾把它删掉过一次（那时域名 302 到 IP，它毫无意义），
#       现在已加回。
#
#    证书**不用动**：域名证书一直是 Let's Encrypt（SAN 覆盖
#    stashbox.cn / www. / api. / admin. 四个），与备案无关；
#    IP 证书（6 天自动续签）继续给 IP 兜底块用。
#
#    安卓端的 IP 兜底**继续保留**，但定位从「备案未过期的必需品」
#    降级为「冗余保险」（域名线路本身出问题时它是唯一退路）。

sudo bash backend/deploy/install-nginx-assets.sh
#    想顺便覆盖主配置（谨慎）：服务器上的 /etc/nginx/nginx.conf 现由
#    patch-nginx-logformat.py 就地打补丁维护，覆盖前会自动备份。
# sudo bash backend/deploy/install-nginx-assets.sh --main
#    只看装没装、差在哪：
# sudo bash backend/deploy/install-nginx-assets.sh --check
#
#    ⚠️ **证书用 tls-cert.sh 选，不要手改 nginx.conf 里的路径**
#
#    为什么需要这个脚本（2026-10-21 实测踩出来的）：
#    安卓 release 包**只信任系统 CA**（network_security_config 的
#    trust-anchors=system），自签证书一律被拒 —— 也就是说：
#      · 用域名 → 证书必须是公网受信的
#      · 用自签证书 → 任何时候都连不上
#    解法：Let's Encrypt 自 2025-12 起支持**签发 IP 证书**（http-01 验证，
#    6 天有效期，自动续签）。IP 没有备案概念 → 不受备案拦截影响，
#    备案期间用 IP 证书，备案通过后换域名证书，同一份 nginx 配置两种证书。
#
#    签发域名证书（acme.sh，**不要用 certbot** —— 这台机器的 certbot
#    续期目录是坏的，签出来的证书到期即死）：
#      bash deploy/issue-domain-cert.sh
#    IP 兜底块用哪张：
#      bash deploy/tls-cert.sh ip
#    看当前用的哪张：
#      bash deploy/tls-cert.sh status
sudo nginx -t && sudo systemctl reload nginx

# ⑨-b 管理后台（静态 SPA，内测必需：推推送 / 看用户 / 管配额）
#    构建（**在开发机上做**）：
cd stashbox-admin-web
pnpm build
#    传到服务器：
ssh root@8.148.9.151 'mkdir -p /var/www/stashbox-admin'
rsync -a dist/ root@8.148.9.151:/var/www/stashbox-admin/
#
#    ⚠️ **不要传 VITE_API_BASE_URL**（2026-10-22 纠正）：
#       bundle 里的 API 地址**编译期就写死**，传了什么就永远是什么。
#       · 现在**不传** = 同源相对路径（/api/v1/admin/…）→ 域名入口和
#         IP 入口（备案期间的临时路径）都能用 —— 两种入口 nginx 都配了 /api/。
#       · 传 https://api.stashbox.cn = 写死域名 → **备案期间用 IP 访问会废了**
#         （请求打到域名 → 阿里云监测系统按未备案域名阻断 → 登录直接失败），
#         而且后台的登录态在 localStorage，跨源时 token 发不过去。
#       · 传 http://localhost:8100 = 生产构建里写死 localhost → 浏览器必然连不上。
#       （dev/e2e 场景例外：e2e 注入 http://127.0.0.1:18100，因为 preview 与 API 不同源。）
#
#    部署后建议顺手清一下旧 chunk（每次构建的 hash 文件名都不同，
#    不清会越攒越多）：
#       ssh root@8.148.9.151 'ls /var/www/stashbox-admin/assets/' | while read f; do
#         grep -qx "$f" <(ls dist/assets/) || echo "$f"
#       done   # 再把列出来的移走

# ⑩ 预检（必须 0 FAIL）
cd backend/deploy && ./preflight.sh /etc/stashbox/stashbox.env
```

细节（服务用户、`ReadWritePaths` 逐条说明、日志、卸载）见
[`deploy/systemd/README`](systemd/README)。

### 9.3 内存预算（实测数字 → 这台机器的分配账）

实测来源：本机（开发机）跑 5 个服务的空闲 / 80 并发内存，以及 ffmpeg 转码峰值。

> ⚠️ **这台机器的物理内存不是 2048MB，是 1613MB。**
>
> 轻量应用服务器标称 2 GiB，但 guest 实测 `free -m` → `Mem: total 1613`。
> 云厂商把一部分内存划给了 hypervisor / 缓存，guest 看到的是 1613。
> 原预算表按 2048M 算，硬上限合计 1920M > 1613M —— 五个单元同时顶满会
> 触发 cgroup 内的 OOM killer，被杀的可能是 PostgreSQL，整机数据面掉线。
> 下面的表已按 1613M 重新分配。

| 组件 | 空闲实测 | 80 并发实测 | 本方案上限 | 依据 |
|---|---|---|---|---|
| api-gateway | 17–40 MB | 60–90 MB | **High = Max 128M** | 只做转发 + JWT 校验；上限 ≈1.4× 实测峰值 |
| user-service | 17–40 MB | 60–90 MB | **High = Max 128M** | bcrypt + PG 连接池 |
| content-service | 17–40 MB | 60–90 MB | **High = Max 224M** | **剪藏路径的稳态峰值**：整页 HTML + 配图下载 + VLM 请求体（见下「剪藏路径」） |
| ai-service | 17–40 MB | 60–90 MB | **High = Max 200M** | LLM 响应 JSON + 音频缓冲 |
| ai-worker | 17–40 MB | 60–90 MB | **High = Max 224M** | 唯一会 fork ffmpeg 的（子进程同 cgroup） |
| **五个常驻进程合计** | **~90 MB** | **~400 MB** | **904M** | MemoryMax 之和 |
| PostgreSQL | 200–300 MB | — | cgroup **High = Max 288M** | shared_buffers 64MB + ≤30 连接 × ~6MB（实测 RSS 23MB）；另配 `Restart=on-failure`（见下） |
| Redis | ~50 MB | — | cgroup **128M** | maxmemory 64mb + 开销 |
| nginx | ~10 MB | — | cgroup **48M** | TLS + 反代 |
| OS + systemd + journald | ~150 MB | — | journald `SystemMaxUse=200M` | 实测 |
| ffmpeg 峰值（转码中） | 18 MB | 18 MB | 计入 ai-worker 的 cgroup | 实测 4MB WAV→64kbps AAC，墙钟 0.35s |
| **合计（硬上限）** | **~550 MB** | **~860 MB** | **1368M / 1613M** | **余量 245M** + 2 GiB swap 兜底 |

> ⚠️ **Python 单元与 PG 的 `MemoryHigh` 一律 == `MemoryMax`** —— 这不是笔误，
> 是 2026-10-25 用对照实验定下来的（见 §9.3.2）：`high < max` 那个区间是
> **死亡陷阱**，跨进去进程就永久挂死；而跨过 `max` 只是被 OOM 杀掉，
> 有 `Restart=always` ⇒ systemd 拉起来（自愈）。**宁可被杀，不要挂死。**
> 硬上限（MemoryMax）没动 ⇒ 这张预算表不受影响。
>
> **PG 尤其要去掉节流区**：它跨过 `high` 的后果是**所有 backend 一起卡住**
> ⇒ 整个数据面挂死（比 Python 服务更糟：连列表查询都拿不到数据）。
> 跨过 `max` 则是 OOM kill，而 postmaster 带 `OOMScoreAdjust=-900`
> （Debian 单元里写死，backend 自己把分数重置为 0）⇒ 内核挑 **backend** 杀，
> postmaster 活下来 ⇒ 那个连接报错、可以重试，PG 本身不受影响。
>
> ⇒ PG 另配了 `Restart=on-failure` + `RestartPreventExitStatus=SIGINT SIGTERM`
> （Debian 单元把 Restart 注释掉了，理由是会破坏 `pg_ctlcluster stop`；
> 配上阻止名单后两条都成立：生产实测 SIGKILL postmaster → 14 秒内自己起来；
> `systemctl stop` / `pg_ctlcluster stop` → 都保持停止，没有重启循环）。
> 见 `tune-host.sh` 里的 `20-restart-on-failure.conf`。
>
> ⚠️ Redis / nginx **保留 high < max**：它们的用量离 high 线还很远
> （Redis 5MB vs 96MB、nginx 9MB vs 24MB），且它们被 OOM 杀掉的影响面小
> （Redis 有 `maxmemory 64mb` 这道数据级上限，nginx 被杀只是入口不通）。
> 要统一之前先单独实测。

### 9.3.1 剪藏路径：content-service 的峰值从哪来（2026-10-25 生产事故）

「空闲 17-40 MB、80 并发 60-90 MB」这组数字**不含剪藏路径** ——
而剪藏才是它内存最重的一条路。实测（一篇 17 图的微信文）：

| 环节 | 修前 | 修后 |
|---|---|---|
| 抓取（3.6MB 整页 + 解析） | +9 MB | +9 MB |
| 配图下载（17 张，合计 9.1MB） | +12 MB | +10 MB |
| **识图（VLM 请求体）** | **+50 MB** | **+5 MB** |
| **稳态边际成本合计** | **+48 MB** | **+21 MB** |

**为什么识图那么肥**：`_ocr_one_image` 把**原始图片字节**直接 base64 塞进请求体，
每张图的字节被复制 4~5 遍（原始 → base64 串 → JSON 串 → utf-8 字节 → 请求体）。
12 张图 ⇒ 瞬时几十 MB。**顺带一提：VLM 的视觉 token 按图面积算 ⇒ 图小了 token 也少。**

⇒ 现在的三道闸门：
1. **识图前缩图**（`ocr.shrink_image_for_vlm`，Pillow）—— 收口在**唯一**拼请求体的那一处；
2. **单篇配图总字节预算**（`media_ocr.MAX_MEDIA_TOTAL_BYTES`）——
   只限张数限不住内存（12×5MB 与 12×50KB 内存差 10 倍）；
3. **请求后归还分配器碎片**（`common/middleware/memory_trim.py`）—— 见下。

**第 3 道闸门是被「静息内存爬升」逼出来的**（实测，同进程连抓 4 次同一篇）：

```
抓取后（gc 跑过）  anon  97 → 103 → 104 → 104 MiB   ← 每次剪藏留一层碎片
malloc_trim 之后   anon  77 →  77 →  77 →  77 MiB   ← 每次都精确回到同一数
```

`gc.collect()` 收不掉（**不是活对象**，是 glibc 手里已 free 的页），
`malloc_trim(0)` 能全部收回 ⇒ 装成中间件，请求结束按阈值还一次。

**生产实测（修复后，连剪 4 次同一篇 17 图微信文）**：

| 指标 | 修前（有缩图、无 trim、High 160M） | 修后（缩图 + 字节闸 + trim，High=Max 224M） |
|---|---|---|
| 静息 cgroup current | 149.7 MiB | **92.6 → 97.0 → 99.3 → 102.5 MiB** |
| 剪藏峰值 | 159.6 MiB | **127.2 → 137.6 → 138.4 MiB** |
| `memory.events` 的 `high` 增量 | **46**（每次剪藏都被节流） | **0**（完全不节流） |
| 剪藏结果 | 200，但过程被节流 | **200 / 30.5s** |

⚠️ 静息值仍有 **+2.5 MiB/次** 的缓慢爬升（探针里不建库、不派蒸馏时是**完全持平**的
⇒ 多出来的那点在「写库 + 派蒸馏」那段，还没定位到）。这正是**不能再把
MemoryHigh 设得比峰值低**的原因：峰值会随剪藏次数漂移，固定的水位线迟早被跨过，
而跨过 `high` 就是挂死（§9.3.2）。⇒ `high == max` 让它变成「被杀 + 自愈」。


### 9.3.2 ⚠️ `MemoryHigh` 是**永久冻死**区，`high == max` 才是可自愈的配置

原预算表里写的是「`MemoryHigh`（软限）触发的是回收压力，不是杀进程；正常流量不会碰到」。
**2026-10-25 的生产事故 + 对照实验把这句话彻底推翻了。**

#### 现象：跨过 `memory.high` ⇒ 永久冻死

cgroup v2 的 `memory.high` 是**节流**——越过水位线的分配会被内核挡在回收路径上，
进程进入 **D 状态**（`wchan=mem_cgroup_handle_over_high`）。后果：

1. 那个请求**永远不返回**（用户看到转圈到天荒地老）；
2. **整个服务都不响应** —— 连 `GET /api/v1/articles`（列表查询）也一起超时；
3. 进程在 D 状态 ⇒ **SIGKILL 都杀不掉** ⇒ 不会自愈、不会自动重启、不会告警；
4. `memory.events` 里 `high 19414`（超水位被节流 19414 次）、`oom 0`
   —— **「没 OOM」不等于「没事」**（压根没走到 OOM killer 那一步）。

#### 对照实验：两种配置各跑一个「持续分配」的进程（生产机，一次性 cgroup）

| 配置 | 进程结局 | `memory.events` | 含义 |
|---|---|---|---|
| `high=64M  max=128M` | 退出码 **124（超时卡死）** | `high 1477` / `max 0` / `oom 0` | 跨过 high ⇒ **永久挂死**，连 OOM killer 都到不了 |
| `high=128M max=128M` | 退出码 **137（SIGKILL）** | `high 0` / `max 19` / `oom 1` / `oom_kill 1` | high==max ⇒ 内核**直接走 OOM 路径** ⇒ 被干净杀掉 |

⇒ **`high < max` 那个区间是死亡陷阱**：跨进去就是挂死；跨过 `max` 只是被杀，
而所有常驻单元都有 `Restart=always` ⇒ 被 systemd 拉起来（**自愈**）。

⇒ **所以本机五个 Python 单元一律 `MemoryHigh == MemoryMax`**：
**宁可被杀，不要挂死。** 硬上限（`MemoryMax`）一个数没改 ⇒ 内存预算表不受影响。

⇒ 判据也要跟着改：
- ❌ 旧判据：「`MemoryHigh` 是软限，触发的是回收压力，不是杀进程」。
- ✅ 新判据：「**`MemoryHigh` 是挂死线**。要么把它设到峰值之上（但峰值会漂移，
  迟早被跨过），要么直接设成等于 `MemoryMax`，让它变成「被杀 + 自愈」」。
  ⇒ **峰值会漂移**这件事本身，就是不该留节流区的理由。

#### 处置（已经卡住了怎么办）

```bash
# ① 确认是不是被节流冻死了
PID=$(systemctl show -p MainPID --value stashbox-content-service)
ps -o pid,stat,wchan:28,rss -p "$PID"      # stat 含 D + wchan=mem_cgroup_handle_over_high ⇒ 就是它
cat /sys/fs/cgroup/system.slice/stashbox-content-service.service/memory.events   # high 计数在涨 = 还在被节流

# ② 先把水位临时抬上去，让被冻结的进程能继续跑完（D 状态杀不掉）
CG=/sys/fs/cgroup/system.slice/stashbox-content-service.service
echo 512M > $CG/memory.high && echo 512M > $CG/memory.max

# ③ 重启服务拿一个干净进程（注意：restart 会把 cgroup 限额重置回 unit 里的值）
systemctl restart stashbox-content-service

# ④ 恢复 unit 里的限额（如果第 ③ 步没自动恢复）
systemctl daemon-reload && systemctl restart stashbox-content-service
```

⚠️ 第 ② 步的抬水位是**急救**，不是修复：只要稳态峰值还是超过水位线，
下一次剪藏会重新冻死 ⇒ 必须同时把代码侧的内存闸门补上（§9.3.1 的三道闸门）。
⚠️ 新的配置下（high==max）这类事故**不该再发生**；真发生了说明有新路径
把内存吃穿了 —— 查 `memory.events` 的 `oom_kill` 与 `memory.peak`，而不是抬水位。

要点：

- **所有硬上限加起来是 1368M ≤ 1613M × 0.85**。所以不会出现"5 个单元同时顶到
  上限 → 内核 OOM 屠机"——最坏情况也是先撞 swap。
- **生产机实测**（5 个服务全部起来后）：
  | 服务 | 实际 RSS | MemoryMax |
  |---|---|---|
  | api-gateway | 75 MB | 128 MB |
  | user-service | 79 MB | 128 MB |
  | content-service | 83 MB（剪藏后 ~103 MB，见 §9.3.1） | 224 MB |
  | ai-service | 72 MB | 200 MB |
  | ai-worker | 89 MB | 224 MB |
  | **合计** | **398 MB** | **904 MB** |

  整机 `free -m`：used 832 / available 781（含 PG + Redis + nginx + OS）。
  内存吃紧的其实是 **PG + Redis + OS**（约 430MB），不是 Python 服务。
- ⚠️ **`MemoryHigh` 不是「回收压力」，是「永久冻死」** ——
  越线 ⇒ 进程卡在 D 状态、请求永不返回、**整个服务都不响应**、且**杀不掉**。
  详见 §9.3.2（含处置步骤）。判据要按「稳态峰值 + 余量」算，不是按空闲/并发算。
- **真不够用时的正确顺序**：先降 PG 的 `max_connections`（30→20）和
  `shared_buffers`（64→48MB），再降 Redis `maxmemory`（64→48mb），
  **最后**才动 Python 单元的 MemoryMax —— PG 被 OOM kill 的代价
  （数据面全断）远大于单个 Python 单元重启。
- `CPUQuota` 之和 = 25+40+35+40+60 = **200%** = 这台机器的 2 vCPU。
  任何单元都不会被饿到 0，也不会独占两个核。
- swap 是**尖峰保险**，不是常规内存：swappiness 压到 10，
  只有真没内存时才换页（`vm.swappiness` 由 tune-host.sh 设置）。

### 9.4 故意**没有**放到这台机器上的东西

| 没放的东西 | 为什么 | 用什么代替 |
|---|---|---|
| Docker / dockerd | 空载就吃 150–200 MB；overlay2 还会把每层再存一份，40 GB 系统盘紧张 | systemd 单元（`deploy/systemd/`） |
| Prometheus + Grafana + Alertmanager + blackbox_exporter + redis_exporter | 合计约 400–500 MB = 2 GiB 的四分之一，而唯一的消费者是"一天看一次图"的人 | **阿里云云监控**（轻量服务器自带 agent，CPU/内存/磁盘指标免费）+ **SLS**（收 journal、查日志、配告警） |
| 本机 IndexTTS | `infra/mlx-audio-tts-service` 是 **macOS/mlx 专属**，而且要加载模型（几个 GB RAM） | `TTS_PROVIDER=edge`（不加载模型，0 RAM）；`indextts` 仍然可用 —— 把 `INDEXTTS_BASE_URL` 指向**另一台**远程 IndexTTS 主机 |
| playwright chromium（无头浏览器抓取通道） | chromium RSS 300 MB+，且是 content-service 的**子进程**（同 cgroup），会直接顶爆它的 MemoryMax | `STASHBOX_FETCH_BROWSER_TIER=0`（代价见下） |
| Langfuse | 第二套埋点后端，2 GiB 机器不需要 | 云监控 / SLS |
| 模型权重（torch / transformers / onnx） | 依赖集里本来就没有 | `edge` TTS + 外部 LLM API |

`infra/docker/docker-compose.observability.yml` **保持原样**（本地开发用），
只是**不要**在这台机器上起它。

### 9.4-b 音频存哪：OSS 还是本地磁盘？（**内测期推荐本地磁盘**）

先纠正一个此前的错误说法：**「武汉没有 OSS 地域」是错的**。
云元数据实测 `region-id = cn-wuhan-lr`（华中1 武汉 · 本地地域），
对应 OSS 端点 `oss-cn-wuhan-lr` / `oss-cn-wuhan-lr-internal`，都存在。

（另：查公网 IP 归属地会得到「杭州」，那是错的 —— 阿里云公网 IP 段按
**注册地**分配。唯一可靠的判据是云元数据：
`curl http://100.100.100.200/latest/meta-data/region-id`。）

**两个方案的实际成本**（内测规模：30 人 × 每天 2 篇 × 30 天 = 1800 篇，
每篇 84 秒的 64k m4a ≈ 0.69 MB）：

| | 方案 A：OSS | 方案 B：本地磁盘 + nginx |
|---|---|---|
| 存储费 | 1.2 GB × 0.12 元 = **0.15 元/月** | 0 元（用系统盘剩余 32 GB） |
| 下行流量 | 1.8 GB × 0.5 元 = **0.91 元/月** ← 大头 | 0 元（轻量 200Mbps **不限月流量**） |
| 请求费 | 5400 次 × 0.01元/万 = **0.01 元/月** | 0 |
| **合计** | **≈ 1.06 元/月** | **0 元/月** |
| 磁盘占用 | — | 1.2 GB / 32 GB（**4%**） |

带宽上也远够：30 人同时听 = 2.0 Mbps，峰值预算是 200 Mbps。

**内测期选方案 B 的理由**：一个月省 1 块钱不重要，但**多一个要开通、
要配 AccessKey、还要盯流量账单的外部依赖就重要** —— 内测期最该少踩依赖。
等有付费用户、或音频量超过 30 GB 时再迁 OSS（代码不用改，只改 env 的
`STORAGE_PROVIDER`）。

**方案 B 的代价（必须知道）**：

1. **音频 URL 是公开直链，没有签名和过期时间**（OSS 默认返回 presigned URL，
   有效期 24h）。缓解：音频 key 是 `audio/{art_<24位随机hex>}.m4a`，
   24 位 hex ≈ 96 bit 熵，**无法枚举**，泄露一条也不会被猜到别的。
2. **单机存储**：磁盘坏了音频就没了。内测期可接受（文章元数据在 PG 里，
   音频可以重新蒸馏）。
3. **需要清理策略**：磁盘会满。加一条 cron 按 mtime 清掉 N 天前的文件
   （或用 logrotate 风格的老文件清理）。

**方案 B 的配置**：

```bash
# env 文件（/etc/stashbox/stashbox.env）
STORAGE_PROVIDER=local
STASHBOX_ALLOW_LOCAL_STORAGE=1     # 不写这个，preflight 会 FAIL
LOCAL_AUDIO_DIR=/var/lib/stashbox/data/audio
LOCAL_STORAGE_PUBLIC_URL=https://api.stashbox.cn/audio
```

网关会自动挂 `/audio` 静态路由（`api-gateway/main.py:163`：
`STORAGE_PROVIDER=local` 或 `ENABLE_LOCAL_AUDIO_MOUNT=1` 时挂载）。

> ⚠️ 走 nginx 对外时，`nginx.conf` 里要**额外**加一段把 `/audio/` 转给网关
> （网关的静态路由在 8100，不在 nginx 的 root 下）：
> ```nginx
> location /audio/ {
>     proxy_pass http://stashbox_gateway;
> }
> ```
> 不加的话客户端拿到 404（nginx 默认 root 下没有这个目录）。

### 9.5 这条路径的已知代价（都是主动换来的）

- **JS 渲染的页面抓不到**：浏览器通道关掉了，纯 HTTP 通道对这类页面
  实测只能抽出 7 个字。要开的话得把 `content-service` 的 MemoryMax 提到 640M，
  2 GiB 的机器上这笔账不成立。
  **但失败是"快速且明确"的**（实测 <1s 返回
  `没能提取出文章正文，页面可能需要登录后才能查看`）——
  因为 `STASHBOX_FETCH_BROWSER_TIER=0` 会让 pipeline 不再升级到浏览器通道。
  ⚠️ 反面：如果这个变量**漏配**（默认是开），playwright 二进制没装 →
     每次剪藏都白等 30 秒抓取预算 → 45 秒超时 + 文章不入库，
     错误信息还含糊（"抓取耗时较长"）。preflight 现在会检查这一项。
  → 内测用户若反馈"某些文章剪藏不了"，就是这个原因，不是服务挂了。
- **本机 PG / Redis 扛不住并发**：这是内测量级的规格。要放量先迁托管 RDS /
  ApsaraDB（改 `POSTGRES_*` / `REDIS_*` 即可，其余不变）。
- **OSS 地域要跟机器对齐**：机器在 `cn-wuhan-lr`（本地地域），OSS 端点可用
  `oss-cn-wuhan-lr`（公网）/ `oss-cn-wuhan-lr-internal`（内网）。
  **实测跨地域的 `-internal` 端点连不上**（武汉机器上 curl
  `oss-cn-hangzhou-internal:443` 直接超时 —— 解析得到 100.118.x.x，
  但流量出不去）。所以 bucket 地域必须选武汉；选不了就退回杭州 bucket +
  杭州**公网**端点（产生出方向流量费，内测量级约 1 元/月）。
  内测期用本地磁盘（9.4-b）则完全绕开这个问题。


---

## 12. 域名切换（ICP 备案期间 / 备案通过后）

### 12.1 两套证书并行（现状）

服务器上**同时**有两套证书，各管各的 server 块：

| server 块 | server_name | 证书 | SAN |
|---|---|---|---|
| 公开面 | `stashbox.cn` `www.stashbox.cn` | 域名证书 | 四个名字（见下） |
| API | `api.stashbox.cn` | 域名证书 | 同上 |
| 后台 | `admin.stashbox.cn` | 域名证书 | 同上 |
| IP 兜底 | `_`（default_server） | **IP 证书** | IP:8.148.9.151 |

域名证书的 SAN 是**一张证书四个名字**：`stashbox.cn` `www.stashbox.cn`
`admin.stashbox.cn` `api.stashbox.cn`。四个 host 复用同一个证书文件 →
这张证书的 SAN 必须同时含四个名字，否则浏览器按 host 校验会直接报
「证书失配」（比拿不到证书更难排查）。

**为什么不能共用一张**（`tls-cert.sh` 的共用片段 `tls-cert.conf`）：
两张证书的 SAN **不重叠** —— 域名证书不含 IP，IP 证书不含域名。
共用必然有一边失配：全用域名证书 → **App 现在的 IP 入口直接连不上**；
全用 IP 证书 → 域名入口失配。所以域名块在 nginx 里**直接写证书路径**，
不走共用片段。

⚠️ 改 nginx 前先备份到 `sites-enabled/` **外面**（`/etc/nginx/backup/`）——
放在 `sites-enabled/` 里会被 nginx 当配置加载，报
`duplicate upstream "stashbox_gateway"`。

### 12.2 签发 / 续期

⚠️ **备案期间域名的 80 端口被阿里云拦截**（见 §12.6 实测），HTTP-01
验证走不通 → 域名证书必须走 **TLS-ALPN-01**（验证走 443，443 不拦）。

```bash
# 签发（ALPN）——会临时停掉 nginx（ALPN 要独占 443），签完自动起回来
bash /opt/stashbox/deploy/issue-domain-cert.sh

# 只看状态，不签发
bash /opt/stashbox/deploy/issue-domain-cert.sh --check

# 续期 + 同步 + reload（cron 每天跑，脚本自己判断是否进窗口）
bash /opt/stashbox/deploy/renew-tls.sh
```

**续期链路的两条岔路**（`renew-tls.sh` 一次跑完）：

| 证书 | 有效期 | 验证方式 | 续期代价 |
|---|---|---|---|
| 域名证书（4 个名字） | 90 天 | **TLS-ALPN-01**（443） | 要停 nginx ~40 秒 |
| IP 证书 | **6 天** | HTTP-01（`8.148.9.151:80`） | 不停 nginx |

两张证书对 nginx 的要求**正好相反**（IP 的验证需要 nginx 跑着，
ALPN 的验证需要 nginx 停着），所以必须分开、按顺序处理，不能用
`acme.sh --cron` 一把梭。

⚠️ **IP 证书的节流**：有效期只有 6 天，但 Let's Encrypt 对 IP 证书的
重复证书配额是**每周 5 张** —— 每天续一张就是 7 张/周，超限。
`renew-tls.sh` 因此把它卡到 **4 天一次**（留 2 天余量，一周 1~2 张）。

⚠️ **不要用 certbot 签域名证书**：之前那两张 `api.` / `admin.` 的证书
是 certbot 签的、**只有 24 小时有效期**，而且 `/etc/letsencrypt/renewal/`
里**没有 renewal 配置** → 到期即死、永远不会续。已归档到
`/root/retired-certs/`。域名证书统一走 acme.sh。

### 12.3 一键切换

```bash
bash /opt/stashbox/deploy/switch-domain.sh status   # 看当前状态
bash /opt/stashbox/deploy/switch-domain.sh domain   # 切到 https://stashbox.cn
bash /opt/stashbox/deploy/switch-domain.sh ip       # 切回 https://8.148.9.151
```

它改**四处**里的三处（第四处不在服务器上）：

1. `env` 的 `PUBLIC_GATEWAY_URL` / `LOCAL_STORAGE_PUBLIC_URL`
   → 决定**新产出**的 `audio_url` 用什么 host
2. `articles.audio_url` + `distilled_articles.audio_url`（**两张表都要**）
   → 存量音频的 URL 在库里，改 env 不会追溯
3. 重启 4 个服务
4. **APK**：base URL 是**编译期常量**（`BuildConfig.API_BASE_URL`，
   来自 `-PapiBaseUrl`），必须**重新构建**并分发给测试者：

   ```bash
   bash scripts/build_release.sh --api-base-url https://stashbox.cn/
   ```

   APK 没重打之前，App 仍连旧地址（旧地址仍可用 —— 这是刻意的回退）。

### 12.4 内测期间：App 走 IP，域名承载营销页 + 后台入口

**为什么 App 继续用 IP**：

- **App 没有多域名容错**：`BaseUrls.gatewayBaseUrl()` 是单一编译期常量，
  域名一断就是**所有内测用户全部连不上**，且 APK 分发后改不了（要重新分发）。
- IP 入口**与备案无关**（IP 证书不需要备案）→ 它就是回退路径。
- 域名的价值是「好看的对外链接」（营销页、分享）+ 后台入口，不是「给 App 用」。

**所以**：域名承载**营销页**（`https://stashbox.cn/`）+ **后台登录**
（`https://admin.stashbox.cn/login`），App 和音频继续走 IP。
备案通过后一条命令切干净（`switch-domain.sh domain` + 重打 APK）。

### 12.5 ⚠️ 关于「域名被按 SNI 封堵」—— 那是误判，别照着改

曾出现过「从某台机器 curl 域名被重置、裸 IP 正常」的现象，当时判断是
「阿里云对未备案域名的入站监测在拦」。

**这个判断是错的**，实测反证：

```
nginx 访问日志（域名块）里已有 13 个不同来源 IP 正常取到内容，
含用户自己的 Windows 浏览器 17:11:02 的 GET / → 200
而那台机器的 curl：ClientHello 发出后被重置，**nginx 侧连错误日志都没有**
→ 重置发生在【到 nginx 之前】，是那台机器出口链路上的东西
   （本地代理 / VPN / 路由器），与备案监测无关
```

**教训**：判断「是不是被某个中间设备拦了」，不能只看客户端现象 ——
要同时看**服务端日志有没有收到这次连接**。没到服务端 = 不在服务端这条链路上。

⚠️ `tls-cert.sh` 顶部那句「备案期间域名解析会被阿里云监测系统阻断」
是**当年的观察**，现在没有复现；用之前先按上面这条验一遍。

### 12.6 备案拦截的真实行为（2026-10-22 实测，别凭印象改）

备案审核期间，阿里云**确实**在拦 —— 但**不是**按 SNI 封 443，而是
**只拦域名的 80 端口**：

| 入口 | 结果 |
|---|---|
| `http://stashbox.cn/…` | **403**，`Server: Beaver`，body 是阿里云备案拦截页 |
| `http://www./admin./api.stashbox.cn/…` | 同上 |
| `http://8.148.9.151/…`（IP，无域名） | **200**，正常放行 |
| `https://stashbox.cn/` `https://admin.stashbox.cn/` | **200**，正常放行 |

拦截页原文：

```html
<title>Non-compliance ICP Filing</title>
… src= "http://www.aliyun.com/beian/beian-block?id=00000000005986618342"
```

**三个推论**（每一条都影响怎么做）：

1. **HTTP-01 验证走不通** → 域名证书只能走 TLS-ALPN-01（443）→ ALPN
   要独占 443 → 续期时必须停 nginx。
2. **443 正常** → 证书能不能签和访问方不通用。证书是「能不能签」，
   备案拦截是「能不能访问」——**这是两件事**，别混。
3. **IP 不受影响** → IP 证书的 HTTP-01 续期照常工作（走
   `http://8.148.9.151/.well-known/…`）。这也是为什么 App 和音频继续走 IP
   是个安全选择。

**诊断要点**：被拦的请求**不会出现在 nginx 访问日志里**（拦截在 nginx
之前）。所以看到 403 就去翻 nginx 日志是找不到证据的 —— 要看响应头
里的 `Server: Beaver` 和 body 里的 `beian-block` 才认得出是备案拦截。

⚠️ 备案通过后 80 端口会解封，那时可以把域名证书切回 HTTP-01
（`issue-domain-cert.sh --webroot`），续期就不用停 nginx 了。
ALPN 继续用也没问题，只是每次续期有 ~40 秒中断。

⚠️ **但切回 HTTP-01 之前先逐个 host 验一遍**：备案拦截是**按域名**判的，
主域名 `stashbox.cn` 解封**不等于**子域名 `admin.` / `api.` 也解封 ——
子域名可能需要在备案系统里单独申报。测法（响应里没有 Beaver 才算通）：

```bash
curl -sI http://admin.stashbox.cn/.well-known/acme-challenge/x | grep -i server
# 出现 "Server: Beaver" → 还在被拦，别切
```

反过来说：**保持 ALPN 就完全不受这件事影响** —— ALPN 走 443，
443 从来没被拦过。

---

## 13. 日志策略（按天 + 按大小 + 压缩归档）

### 13.1 生产（8.148.9.151）：三层日志，各管各的

| 层 | 落在哪 | 谁管 | 策略 |
|---|---|---|---|
| 应用（四个 uvicorn + arq worker） | **journald** | `tune-host.sh` 写的 `/etc/systemd/journald.conf` | `SystemMaxUse=200M`（总量）/ `SystemMaxFileSize=50M`（单文件）/ `MaxRetentionSec=30day`（按天）/ `Storage=persistent`。journal 本身是 zstd 压缩 → 「压缩归档」journald 自带 |
| nginx 访问/错误日志（4 个 vhost） | `/var/log/nginx/*.log` | 系统的 `/etc/logrotate.d/nginx` + `install-log-policy.sh` 补的一行 | `daily` + `maxsize 100M` + `rotate 14` + `compress` + `delaycompress` |
| 我们自己的文件日志 | `/var/log/stashbox/*.log` | `/etc/logrotate.d/stashbox`（`install-log-policy.sh` 装） | 同上，`maxsize 50M` |

**策略本体在 `deploy/install-log-policy.sh`（唯一真相源）**，幂等、可重复执行、可 `--dry-run`：

```bash
bash backend/deploy/install-log-policy.sh --dry-run   # 先看它要干什么
bash backend/deploy/install-log-policy.sh             # 真装（含 logrotate -d 校验）
```

`install-log-policy.sh` 干三件事：建日志目录、写 `/etc/logrotate.d/stashbox`、
给系统的 nginx 策略补 `maxsize`。

⚠️ **两条不能踩的坑**：

1. **不能给同一个文件写两条 logrotate 规则** —— logrotate 会报
   `duplicate log entry` 并**整轮放弃**（nginx 的日志就全都不轮转了）。
   所以「按大小」是**加进系统已有的 nginx 那一段**，不是另起一段。
2. **必须 copytruncate** —— 服务持有日志的 fd，只改名的话它会继续往
   已改名的文件里写（老文件继续长），等于没轮转。

**实测**（2026-10-22，强制轮转验证）：归档 `.2.gz` 生成正常，1.45MB → 59KB
（压缩率 ~24x）；轮转后 nginx 继续正常写日志（HTTP 200 + 日志增长）；
`logrotate -d` 两段都通过。

### 13.2 本机（macOS 开发环境）：newsyslog 用不了，改用 copytruncate 脚本

本机后端服务是**手工起的 uvicorn**，日志重定向到 `/private/tmp/stashbox-*.out`
（实测一轮就攒到 335MB，且**没有上限**）。

macOS 的 `newsyslog` 是「改名」语义，这些进程持有 fd → 改名后继续往已改名的
文件里写 → 归档一直长 → 等于没转；让它重开文件得发信号，而手工起的进程
没有稳定 pidfile。

所以改用 `deploy/local/rotate-dev-logs.sh`（压缩一份 + 截断原文件，fd 不用动），
并用 launchd 每天跑一次：

```bash
cp backend/deploy/local/com.stashbox.logrotate.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.stashbox.logrotate.plist
bash backend/deploy/local/rotate-dev-logs.sh   # 立刻跑一次
```

阈值 50MB，归档只留**最近 20MB**（gzip 后 ~700KB）——开发日志的价值在
「最近发生了什么」，开头的几百 MB 基本是噪音。

---

## 14. 音频文件 ↔ DB 引用的一致性（删生产音频前必读）

### 14.1 背景：2026-10-22 误删 6 篇在用文章的音频

做「清理孤儿音频」时，误删了 6 篇文章**仍在使用**的音频文件
（用户点播放 → 404）。事后复查还原：其中 5 个能从备份按**内容寻址名**
精确还原（sha256 逐字节一致），第 6 个全盘无副本，只能用生产同款 TTS
重新合成（229.3s vs 原 242s）并把新 URL 写回两张表。

**根因不是手滑，是判据不完整**：

| 错在哪 | 后果 |
|---|---|
| 参考集手写死表名/列名，漏了 `article_audio_variants` | 那类文件被判成「没人引用」 |
| 文件名提取方式各家不同（URL 取末段 vs oss_key 去前缀） | 引用集合与磁盘集合不可比 |
| **在已经发现「引用缺失」的情况下仍按「无主文件」清单删除** | 参考集不可信时，「没人引用」这个结论同样不可信 → 删了正在用的文件 |

### 14.2 判据已缝成一份契约（唯一真相源）

`backend/scripts/verify_audio_consistency.py` —— 仓库内工具，不许再手写半套参考集：

- **参考集自动发现**：查 `information_schema`，把所有 public 表里叫
  `audio_url` / `oss_key` / `audio_path` 的列全部纳入（表名硬编码会漂移）。
- **键归一化唯一**：URL（含 query）/ oss_key / 文件名 → 同一个 basename。
- **双向报告**：`引用缺失`（用户播放 404）与 `无主文件`（占磁盘）分开列。
- **联锁**：只要有引用缺失 → **禁止**任何删除动作（退出码 2）。
- **只移不删**：清理是「移到隔离目录」，不是 rm；默认 dry-run，要 `--yes` 才执行。
- **有引用缺失时退出码非 0** → 可挂巡检。

```bash
# 只读核对（生产上这么跑）
cd /opt/stashbox/stashbox/backend/ai-service
sudo -u stashbox /opt/stashbox/venv/bin/python \
  /opt/stashbox/stashbox/backend/scripts/verify_audio_consistency.py

# 真清理无主文件（先看清单，再加 --yes）
... verify_audio_consistency.py --delete-orphans --yes
```

护栏在 `backend/tests/infra/test_verify_audio_consistency.py`（18 例）：
键归一化、双向判定、联锁（生产事故形状的回归用例）、参考集不硬编码表名、
只移不删、CLI 默认只读。

### 14.3 音频文件名的内容寻址约定

`audio/{article_id}.{sha256(bytes)[:8]}.{ext}`（`app/services/storage/base.py:versioned_audio_key`）。
音频重生成 → key 变 → URL 变 → 客户端（Media3 SimpleCache 按 URL 缓存且
**永不重新验证**）缓存自然失效。

⚠️ 由此产生两个必须知道的点：

1. **同一篇文章的历史版本会变成孤儿**（文件名带旧 hash）—— 这属于正常产物，
   用上面的核对器判定后再清。
2. **还原/改名时必须校验内容 hash**：文件名里的 hash8 是内容的 sha256 前 8 位，
   对得上才说明是同一份内容（2026-10-22 还原 5 个文件时用的就是这个判据）。

---
