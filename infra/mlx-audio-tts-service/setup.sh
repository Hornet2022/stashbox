#!/usr/bin/env bash
# setup.sh — 重建听匣独立 TTS 服务的 Python 环境。
#
# 为什么要自建（而不是直接用 oMLX）：
#
#   oMLX 的 TTS 引擎内部**就是** mlx-audio（`omlx/engine/tts.py:116` 写着
#   `from mlx_audio.tts.utils import load_model`），但那 455 行封在一个
#   1.6GB 的签名 app 里。它自己踩过的坑（内部编号 #2312）我们无法修，只能等发版。
#   把 mlx-audio 单独装出来自己接管之后，那层修复就成了本仓库里的普通代码。
#
# 三条硬约束都满足：
#   · 可复用现有模型 —— 权重仍然读 /Volumes/AIWorker，一个字节都不用搬
#   · 轻量         —— venv 562MB vs oMLX.app 1.6GB
#   · 支持并发     —— 端点契约与 oMLX 完全一致，并发开关交给调用方
#
# 实测（2026-10-01，同一段 38 字文本，机器空闲）：
#   oMLX  :8000   24.2s  RTF 2.85x  4.48 字/秒
#   本服务 :8010  23.5s  RTF 2.80x  4.52 字/秒   （冷启动首次 28.0s）
#   → 换栈**不提速**，买的是可控性。
#
# 用法：
#   bash infra/mlx-audio-tts-service/setup.sh [VENV_DIR]
set -euo pipefail

VENV_DIR="${1:-/Users/hornet/work/.venvs/mlx-audio}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# oMLX 自带的 cpython-3.11。本机系统 python 是 3.14，mlx 还没跟到。
BOOTSTRAP_PY="/Applications/oMLX.app/Contents/Resources/Python/cpython-3.11/bin/python3.11"
if [[ ! -x "$BOOTSTRAP_PY" ]]; then
  echo "✗ 找不到 $BOOTSTRAP_PY" >&2
  echo "  需要一个 python 3.11：装 mlx-audio 要求 <3.14，或改用别的 3.11 解释器。" >&2
  exit 1
fi

echo "▶ 建 venv: $VENV_DIR"
mkdir -p "$(dirname "$VENV_DIR")"
[[ -d "$VENV_DIR" ]] || "$BOOTSTRAP_PY" -m venv "$VENV_DIR"

PY="$VENV_DIR/bin/python"

echo "▶ 装 mlx-audio（服务端依赖）"
# 注意：`mlx-audio[server]` 会连带装 webrtcvad，那是个需要本地编译 C 扩展的包，
# 在没装 Xcode CLT 的机器上必然失败（Failed building wheel for webrtcvad）。
# 而它只在实时语音转写路由 /v1/audio/transcriptions/realtime 里用到，与 TTS 无关。
# 所以这里**不装 [server] extra**，改成分开装真正需要的 fastapi/uvicorn，
# webrtcvad 用本目录的占位模块顶上（调用到那个路由会显式报错而不是静默出错）。
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet "mlx-audio==0.4.6"
"$PY" -m pip install --quiet fastapi "uvicorn[standard]" python-multipart

echo "▶ 装本仓库的服务代码到 venv 的 site-packages"
SITE="$("$PY" -c 'import site; print(site.getsitepackages()[0])')"
for f in tts_serve.py omlx_sampling.py webrtcvad.py; do
  cp "$SRC_DIR/$f" "$SITE/$f"
  echo "  $f"
done

echo "▶ 校验"
"$PY" - <<'PY'
import importlib.metadata as md
import mlx.core as mx
import mlx_audio
from mlx_audio.tts.utils import MODEL_REMAPPING, get_available_models
print(f"  mlx-audio {md.version('mlx-audio')}  mlx {md.version('mlx')}  device={mx.default_device()}")
print(f"  qwen3_tts 已支持: {'qwen3_tts' in MODEL_REMAPPING}   TTS 架构数: {len(get_available_models())}")
PY

cat <<EOF

✅ 完成。启动：

  MLX_AUDIO_MODEL_ALIASES="Qwen3-TTS-12Hz-0.6B-Base-bf16=/Volumes/AIWorker/mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16" \\
    $PY -m tts_serve --host 127.0.0.1 --port 8010

开机自启用 launchd（plist 已在本仓库 infra/launchd/）：

  cp infra/launchd/com.stashbox.tts-serve.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.stashbox.tts-serve.plist

后端切换（backend/.env）：

  INDEXTTS_BASE_URL=http://127.0.0.1:8010/v1
  INDEXTTS_SINGLE_INSTANCE=1      # 不设的话 _sibling_omlx_urls 会把 8000 补回故障切换链

回滚：把 INDEXTTS_BASE_URL 改回 http://127.0.0.1:8000/v1 并重启 ai-worker。
oMLX 仍在 8000 托管 embedding / OCR / rerank，全程没动。
EOF
