# syntax=docker/dockerfile:1.7
#
# Stashbox 后端生产镜像
# ======================
#
# 一个 Dockerfile，6 个 target：
#   builder        装依赖（poetry，严格按 poetry.lock）
#   runtime        共享基础镜像（ffmpeg + 非 root 用户 + 源码）
#   content        runtime + 无头浏览器（playwright chromium）
#   api-gateway    runtime + 8100 端口
#   user-service   runtime + 8101 端口
#   content-service content + 8102 端口
#   ai-service     runtime + 8103 端口
#   worker         runtime + arq worker（无端口）
#   migrate        runtime + alembic upgrade head（一次性）
#
# ── 为什么一个镜像给 4 个服务（而不是 4 个 Dockerfile）──────────────────
# 4 个服务的依赖集合几乎完全相同（fastapi / uvicorn / sqlalchemy / asyncpg /
# redis / jose / passlib / loguru / structlog / prometheus-client …），
# 真正的差异只有两处：
#   · content-service 额外要 playwright chromium（浏览器二进制 ~150MB）
#   · ai-service 额外要 langgraph / langfuse / arq / mcp / edge-tts
# 拆成 4 个镜像的代价：4× 构建时间、4× 仓库存储、4 份依赖漂移风险，
# 而省下来的体积只有 5% 左右（因为大头 sqlalchemy/langgraph 大家都装）。
# 所以：共享 runtime + 1 个 content 变体，是"体积 vs 构建复杂度"里更划算的一侧。
# 若将来某个服务的依赖明显分叉，再单独拆 Dockerfile 不迟。
#
# ── base image 为什么是这个 ────────────────────────────────────────────
# python:3.13-slim-bookworm
#   · 3.13 = backend/.venv 实际验证过的解释器（3.13.12），pyproject 声明 ^3.11
#   · slim = 约 150MB 基础层，不是 full 的 1GB+
#   · bookworm = Debian 12，ffmpeg/playwright 的系统依赖都有现成包
# ⚠️ 这个 tag 是"小版本滚动 tag"，每次上游 patch 重建都会变内容。
#    正式发版请改成精确 patch（python:3.13.12-slim-bookworm）甚至 digest：
#        python:3.13.12-slim-bookworm@sha256:<digest>
#    （本机无法访问 registry-1.docker.io / auth.docker.io，取不到 digest，
#      所以这里不写一个编造的 digest 值 —— 编造的 digest 比滚动 tag 更危险。）
#
# ── PYTHONPATH 为什么是 /app ──────────────────────────────────────────
# 代码里全部用绝对导入 `stashbox.backend.common.config`：
# `stashbox` 是**仓库根目录**这个包（stashbox/__init__.py），
# `backend` 是它下面的子包（backend/__init__.py）。
# 也就是说 `stashbox` 包必须能被 import 到 → 仓库根的**父目录**要在 sys.path 上。
# 本机 run_dev.sh 就是这么做的：
#     REPO_PARENT="$(dirname "$REPO_ROOT")"; export PYTHONPATH="$REPO_PARENT:..."
# 本镜像的 build context 是 backend/（根目录的 __init__.py 在 context 之外），
# 所以在镜像里把布局还原成：
#     /app/stashbox/__init__.py          ← 镜像内重建（见下方 RUN printf）
#     /app/stashbox/backend/...          ← 从 context 复制
#     PYTHONPATH=/app                    ← 于是 `import stashbox.backend.*` 成立
#
# ── 工作目录为什么是各服务自己的目录 ──────────────────────────────────
# api-gateway / user-service / content-service / ai-service 都被
# `python -m uvicorn main:app` 启动（run_dev.sh 同样写法）：
#   · `main:app` 要从 CWD 找 main.py
#   · `python -m` 会把 CWD 放进 sys.path[0]，服务内 `from config import ...`
#     （api-gateway/config.py）、`from arq_settings import ...`、
#     `from observability.metrics import ...` 这类顶层模块导入才成立
# alembic 必须 CWD=backend/：alembic.ini 里 script_location = alembic 是相对 CWD 的。

ARG PYTHON_IMAGE=python:3.13-slim-bookworm

# ─────────────────────────────────────────────────────────────────────
# builder：按 poetry.lock 装依赖（和 CI 的 `poetry install` 同一套解析结果）
# ─────────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_VERSION=2.5.1

# 装在独立 venv 里：runtime 阶段整体 COPY 过去，不污染系统 site-packages，
# 也不用在 builder / runtime 之间对齐 site-packages 里的散装 .pth 文件。
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# poetry 2.5.1 ≥ 2.3.4：修掉了 2.1.x 的 wheel/sdist 路径穿越
# （CVE-2026-34591 / CVE-2026-41140）。别降到 2.1.x。
RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

WORKDIR /src
# 只拷这两个文件 → 依赖没变时这一层命中缓存，不重装。
COPY pyproject.toml poetry.lock ./

# --only main：不要 dev 组（pytest/ruff/mypy/aiosqlite…），生产镜像不带测试工具。
# --no-root  ：package-mode = false，本来也不会装根包，这里写明确一点。
RUN poetry install --only main --no-root \
 && python -c "import fastapi, uvicorn, arq, sqlalchemy, asyncpg, redis, boto3, structlog, loguru; print('runtime deps import OK')"

# ─────────────────────────────────────────────────────────────────────
# runtime：4 个服务 + worker + migrate 的共享基础镜像
# ─────────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app \
    TZ=Asia/Shanghai \
    FFMPEG_BIN=/usr/bin/ffmpeg \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    LOCAL_AUDIO_DIR=/var/audio \
    INDEXTTS_AUDIO_DIR=/app/stashbox/backend/ai-service/data/audio

# ffmpeg/ffprobe 是**运行时硬依赖**，不是可选：
#   · ai-service/distill/steps.py::_find_bin 找它（PATH + /usr/bin 等）
#   · ai-service/distill/audio_variant_service.py::transcode_bytes 真的 fork 它
#     （多码率 m4a 转码，失败则整条降级链断掉）
#   · app/services/tts/indextts.py 拼音频时也用
# CI 同样在跑测试前装 ffmpeg（见 .github/workflows/test.yml），理由一致。
# curl：给 healthcheck 和排障用（slim 镜像默认没有）。
# tzdata：让 zoneinfo 能用（容器默认 UTC，TZ 设为 Asia/Shanghai 对齐本机行为；
#         需要与 RDS 的时区一致，见 deploy/README.md）。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# 非 root 运行。uid/gid 固定 10001，方便 ACK 里配 runAsUser / fsGroup。
RUN groupadd --system --gid 10001 stashbox \
 && useradd --system --uid 10001 --gid stashbox --home-dir /app --shell /usr/sbin/nologin stashbox

WORKDIR /app

# 源码：build context 是 backend/ → 还原成 /app/stashbox/backend/
COPY --chown=stashbox:stashbox . /app/stashbox/backend/

# 仓库根包标记（stashbox/__init__.py 在 context 之外，镜像里重建）
RUN printf '"""stashbox - 听匣 AI 音频订阅平台（仓库根包，容器内重建）"""\n' \
        > /app/stashbox/__init__.py \
 && mkdir -p /app/stashbox/backend/ai-service/data/audio /var/audio \
 && chown -R stashbox:stashbox /app /var/audio \
 && chmod -R u+rwX,g+rwX /app/stashbox/backend/ai-service/data /var/audio

USER stashbox

# 供 compose / K8s 覆盖。TRUSTED_PROXY_IPS 只填**真正**会传
# X-Forwarded-For 的上游（SLB / Ingress 网段）。
# 填 "*" = 任何人都能伪造来源 IP → 登录限流（按 IP）形同虚设。
# preflight.sh 在 prod 下会拒绝 "*"。
ENV TRUSTED_PROXY_IPS=127.0.0.1

# ─────────────────────────────────────────────────────────────────────
# content：runtime + 无头浏览器（剪藏 Tier 2 兜底通道）
# ─────────────────────────────────────────────────────────────────────
FROM runtime AS content

USER root
# playwright 的浏览器二进制**不随 pip 安装**（~150MB），要单独装。
# --with-deps 会把 chromium 缺的系统库（libnss3 / libatk…）一并 apt 装上。
# 浏览器装在 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright（不是 /root/.cache），
# 这样非 root 用户也读得到。
# 装完删掉 apt 索引和 root 的 pip 缓存。
RUN playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache/pip \
 && chmod -R a+rX /ms-playwright
USER stashbox

# ─────────────────────────────────────────────────────────────────────
# 各服务 target
# ─────────────────────────────────────────────────────────────────────
FROM runtime AS api-gateway
WORKDIR /app/stashbox/backend/api-gateway
EXPOSE 8100
# /readyz 会探 PG + Redis（common/observability.py）——比 /health 更能反映"能不能干活"。
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8100/readyz',timeout=4).status==200 else 1)"
# --proxy-headers：网关要拿到**真实**客户端 IP 才能给下游补 X-Forwarded-For
#   （下游的登录限流、审计日志都按这个 IP 计数，见 api-gateway/main.py 的 proxy()）。
#   但只信 TRUSTED_PROXY_IPS 白名单里的上游 —— 填 "*" 等于让公网客户端
#   自己伪造 XFF 绕过限流。默认 127.0.0.1 = 谁都不信（保守）。
CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port 8100 --proxy-headers --forwarded-allow-ips \"${TRUSTED_PROXY_IPS:-127.0.0.1}\""]

FROM runtime AS user-service
WORKDIR /app/stashbox/backend/user-service
EXPOSE 8101
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8101/readyz',timeout=4).status==200 else 1)"
CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port 8101 --proxy-headers --forwarded-allow-ips \"${TRUSTED_PROXY_IPS:-127.0.0.1}\""]

FROM content AS content-service
WORKDIR /app/stashbox/backend/content-service
EXPOSE 8102
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8102/readyz',timeout=4).status==200 else 1)"
CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port 8102 --proxy-headers --forwarded-allow-ips \"${TRUSTED_PROXY_IPS:-127.0.0.1}\""]

FROM runtime AS ai-service
WORKDIR /app/stashbox/backend/ai-service
EXPOSE 8103
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8103/readyz',timeout=4).status==200 else 1)"
CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port 8103 --proxy-headers --forwarded-allow-ips \"${TRUSTED_PROXY_IPS:-127.0.0.1}\""]

# Arq worker：蒸馏队列消费者。没有 HTTP 端口 → 不配 healthcheck
#（compose 的 restart 策略负责拉起；进程退出会被 restart: unless-stopped 接管）。
FROM runtime AS worker
WORKDIR /app/stashbox/backend/ai-service
# 队列名 stashbox:distill 在 ai-service/arq_settings.py 里写死，这里不用传。
CMD ["python", "-m", "arq", "worker.WorkerSettings"]

# 迁移：必须**先于**所有服务跑完（compose 里用 service_completed_successfully 卡住）。
# CWD 必须是 backend/：alembic.ini 的 script_location = alembic 是相对 CWD 的。
FROM runtime AS migrate
WORKDIR /app/stashbox/backend
CMD ["alembic", "upgrade", "head"]
