# 听匣独立 TTS 服务

用 `mlx-audio` 直接提供 OpenAI 兼容的 TTS，取代 oMLX 承担 TTS 合成。
**不是换栈提速，是把修复权拿回自己手里。**

## 为什么不用 oMLX

oMLX 的 TTS 引擎内部**就是** `mlx-audio`：

```
/Applications/oMLX.app/Contents/Resources/omlx/engine/tts.py:116
    from mlx_audio.tts.utils import load_model as _load_model
```

那 455 行是一层 HTTP 引擎壳（外加 92 行 sampler 补丁），封在一个 1.6GB 的
**签名 app** 里。它自己踩过的坑我们改不了，只能等发版 —— 上一轮「空转 /
13 字短文本也零响应」的排查就卡在这里。

把它单独 pip 装出来自己接管之后，同一个修复就成了本目录里的普通代码。

## 三个文件

| 文件 | 作用 |
|---|---|
| `tts_serve.py` | 服务入口，在 `mlx_audio.server` 之上打三层补丁后启动 |
| `omlx_sampling.py` | 免编译 sampler 实现（从 oMLX 原样拷贝，Apache-2.0） |
| `webrtcvad.py` | 占位模块，见下方「webrtcvad」 |

venv 里只装依赖；重建用 `bash setup.sh`。

## 三层补丁

### 1. `#2312` sampler 冻结 RNG（必须，否则会踩「空转 / 零音频」）

`mlx_lm/sample_utils.py:137/162/212/232` 把 `categorical_sampling` /
`apply_top_k` / `apply_top_p` / `apply_min_p` 都装饰了：

```python
@partial(mx.compile, inputs=mx.random.state, outputs=mx.random.state)
```

这个装饰器在第一次调用后**不再推进全局 RNG 状态**。TTS sampler 于是重放同一个
冻结的随机数；当冻结值偏向 codec EOS 列时，talker 在 step 0 就发 EOS，此后每次
`/v1/audio/speech` 都返回**零音频**。因为 RNG 状态和 compile 缓存是进程级的、
跨模型重载存活的，**只有重启进程才能恢复** —— 表现就是 50% CPU 空转、连 13 字
短文本也零响应。

修法是把这四个函数换成 `omlx_sampling.py` 里的免编译版本（同样的实现，去掉
`mx.compile` 装饰器），并按**对象身份**扫描已 import 的 backend 模块重绑
——用身份而不是属性名，是为了不误伤 `moss_tts` 那类自己定义了同名 sampler
的后端。

### 2. 模型别名解析（不加会静默下载几十 GB）

`mlx_audio.utils.get_model_path` 的语义是「本地不存在就从 HuggingFace 拉」。
后端发的是短名 `Qwen3-TTS-12Hz-0.6B-Base-bf16`，本地并没有这个目录，
直接调会触发下载。用 `MLX_AUDIO_MODEL_ALIASES` 先把短名映射成本地绝对路径，
权重继续从 `/Volumes/AIWorker` 读，**不搬任何模型文件**。

### 3. `ref_audio`：base64 → 文件

后端按 oMLX 的契约发 `ref_audio` = base64 音频字节；而
`mlx_audio.server` 的 `SpeechRequest.ref_audio` 期望**文件路径**
（`server.py:601` 不存在就 400）。这层转换原本由 oMLX 在自己的引擎壳里做。

ASGI 中间件在 FastAPI 解析 pydantic 之前把 body 里的 base64 落盘
（按内容 sha256 命名，同一个参考音频进程生命周期内只写一次），替换成路径。
后端一行不用改，**回滚只需把 `INDEXTTS_BASE_URL` 改回 8000**。

同一个中间件还负责把缺省的 `response_format` 补成 `wav` ——
`SpeechRequest` 默认是 `mp3`，而后端用 `wave.open` 解析返回字节。

## webrtcvad

`mlx-audio[server]` 依赖 `webrtcvad`，那是个需要本地编译 C 扩展的包，在没装
Xcode CLT 的机器上必然失败。它只在实时语音转写路由
`/v1/audio/transcriptions/realtime` 用到，与 TTS 无关，所以 `setup.sh` 改装
真正的 `fastapi` / `uvicorn`，`webrtcvad` 用本目录的占位模块顶上
（import 期可用，调用到那个路由时显式报错，不静默出错）。

## 实测（2026-10-01，本机 16GB，同一段 38 字文本，机器空闲）

| | 墙钟 | 音频 | RTF | 语速 |
|---|---|---|---|---|
| oMLX `:8000` | 24.2s | 8.5s | 2.85x | 4.48 字/秒 |
| 本服务 `:8010` | 23.5s | 8.4s | 2.80x | 4.52 字/秒 |
| 本服务冷启动首次 | 28.0s | 8.4s | 3.33x | 4.52 字/秒 |

**换栈不提速。** 同样 1.7GB 权重、同样 GPU 访存、同样 16GB 内存墙。

并发也是同理 —— 受控重测（同一段文本）：

| | 单请求墙钟 | 产出音频 |
|---|---|---|
| 单发 | 24.2s | 8.5s |
| 并发 2 | 各 49.3s | 合计 16.8s |
| 并发 3 | 各 66.0s | 合计 25.4s |

2/3 路全部成功、进程存活，但 49.3 ≈ 2×24.2、66.0 ≈ 3×22 → 推理被完全串行化，
**并发零吞吐增益**，单流已打满 GPU 访存。所以后端 `ARQ_MAX_JOBS=1`
和 TTS 串行都应该保持。

## 运维

```bash
# 状态
launchctl list | grep tts-serve
curl -s http://127.0.0.1:8010/v1/models

# 日志
tail -f /tmp/stashbox-tts-serve.out

# 重启
launchctl kickstart -k gui/$(id -u)/com.stashbox.tts-serve

# 停（后端会因熔断在几秒内明确失败，而不是烧 16 分钟）
launchctl bootout gui/$(id -u)/com.stashbox.tts-serve
```

plist 在 `../launchd/com.stashbox.tts-serve.plist`，
`KeepAlive` 用 `SuccessfulExit=false`（只在非零退出时重启）+ `ThrottleInterval=30`
—— oMLX 自己的 plist 因为 `KeepAlive=true` 撞上 daemonize，拉出过 4 个实例把内存打爆。

**服务重启会中断正在跑的合成**，后端的熔断会捕获（连续 3 段失败即开闸，
`INDEXTTS_BREAKER_COOLDOWN=300`），但那一篇会失败。不要在蒸馏跑的时候重启它。

## 回滚

后端 `backend/.env`：

```diff
- INDEXTTS_BASE_URL=http://127.0.0.1:8010/v1
+ INDEXTTS_BASE_URL=http://127.0.0.1:8000/v1
```

然后重启 `com.stashbox.ai-worker`。oMLX 全程没动过，仍在 8000 托管
embedding / OCR / rerank，权重也一直在原地。
