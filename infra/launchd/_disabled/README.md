# 已停用的 launchd agent

这里存的是**不再自动启动**的服务 plist，保留是为了随时能恢复。

## com.openclaw.omlx.plist（2026-10-01 停用）

oMLX 已从听匣彻底弃用。它的 TTS 引擎内部本来就是 mlx-audio，那 455 行封在
1.6GB 的签名 app 里改不了；TTS 现在由 `com.stashbox.tts-serve`（:8010）承担。

停用时它的模型已经重新分工：

| 能力 | 现在的归属 |
|---|---|
| TTS | 自建 mlx-audio 服务 `:8010` |
| embedding | KnowledgeBase 的 mlx-lm 自写服务 `:8008`（早就不走 oMLX） |
| OCR / rerank | 无调用方 |

## 要恢复

```bash
cp infra/launchd/_disabled/com.openclaw.omlx.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.omlx.plist
curl -s http://127.0.0.1:8000/v1/models
```

注意该 plist 的 `RunAtLoad` 是 `false`（2026-10-01 之前手工关掉的开机自启），
恢复后要手动 `launchctl kickstart` 或把 `RunAtLoad` 改回 `true`。

⚠️ 恢复意味着 oMLX 会重新预加载 `IndexTTS-1.5`（+1.7GB），除非先在
`~/.omlx/model_settings.json` 里把它的 `is_pinned` 改回 `false`。

⚠️ 恢复也会把「mx.compile 冻结 RNG → TTS 返回零音频 → 只有重启进程才恢复」
那个坑（oMLX 内部编号 #2312）一起带回来，而且改不了。
