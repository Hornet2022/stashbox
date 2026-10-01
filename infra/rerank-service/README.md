# 听匣本地 rerank 服务

用 `mlx-lm` + `Qwen3-Reranker-0.6B-4bit` 提供 OpenAI 风格的 `/v1/rerank`，
监听 `127.0.0.1:8011`。**和 TTS 服务共用同一个 venv**（`/Users/hornet/work/.venvs/mlx-audio`），
权重仍读 `/Volumes/AIWorker`，不搬任何模型文件。

## 为什么是 Qwen3-Reranker，不是 jina-reranker

本机有两个 reranker，**只有一个能用**，而不能用的那个失败方式极其危险。

`jina-reranker-v3.5-mlx` 的 `model_type` 也是 `qwen3`，所以 `mlx_lm.load()` 会
**成功加载、`forward` 也会成功**——但它的权重里有 `projector.*` 两个 tensor，
`mlx_lm.models.qwen3.Model` 不认识会**静默丢弃**；而且它是 cross-encoder 架构，
官方是用 `[CLS]` hidden + projector 算相似度，不是 yes/no logprob。

实测（2026-10-01，同一组文档，同一个 query）：

| 文档 | jina | Qwen3-Reranker |
|---|---|---|
| 相关（退款流程） | +0.656 | **+1.375** |
| 半相关（退款政策） | +1.195 | −2.500 |
| 不相关（天气） | +0.344 | **−11.219** |

jina 那一列**排序完全颠倒、区分度接近零**。换成它会得到「能跑、结果是乱的」
假象，比直接报错难查得多。

`Qwen3-Reranker-0.6B-4bit` 声明的是 `Qwen3ForCausalLM`，mlx-lm 原生支持。

## 打分方式

Qwen3-Reranker 官方做法：把 `(query, document)` 套进「只能回答 yes / no」的
指令模板，取末位 `yes` 与 `no` 两个 token 的 log-prob 之差
`logP(yes) - logP(no)`。分数越大越相关，可为负。

这个模型**没有 chat_template**，模板是手写的（见 `rerank_serve.py` 的 `_TEMPLATE`）。

## 两个踩过的坑

### 1. 取 logits 的位置

decoder-only 里 `logits[:, i, :]` 预测的是**位置 i+1** 的 token。批处理时为了
对齐会做 padding，如果图省事取 `logits[:, -1, :]`，那一列是 **pad 位置**的输出，
等于在问「pad 之后是什么」。

实测踩过：15 条一批时分数最大偏差 14.5，而且「苹果公司发布了新款平板」这种
完全无关的文档被排到第 5 名——看起来能跑、结果是乱的。

正确做法是取每条自己那一段的**最后一个真实位置**（`rerank_serve.py` 里的
`last_real`）。用**右 padding**：pad 排在真实 token 之后，causal mask 下真实
token 看不到它们，所以不需要 attention mask，position 也天然正确。

### 2. MLX 高级索引

别写 `logits[mx.array(last_real), :, :]`——MLX 的高级索引语义和 numpy 不一样，
实测会 squeeze 掉中间维并抛 `Cannot squeeze axis 1`。这里 batch 最多 16，
逐条 `mx.stack` 的开销可以忽略。

## 精度

`batch=16` 整批 与 `batch=1` 逐条 的分数最大偏差 **0.5625**，但：

- 偏差全是 2 的幂次步进（0.031 / 0.062 / 0.125 / 0.188 / 0.312 / 0.5625），
  是 **4bit 量化的浮点精度**，不是 padding 污染；
- 15 条的降序排列在两种模式下**完全一致**（唯一差异是两条文档都恰好得 2.062
  时的 tie-break）。

分数范围约 −12 ~ +2.4，所以 0.56 的绝对偏差不到量程的 4%。

## 性能

| 场景 | 耗时 |
|---|---|
| 模型加载 | 0.4s |
| 单条 | ~40ms |
| 15 条一批 | ~500ms（含首次预热，稳定态更快） |

## 接口

```bash
curl -s http://127.0.0.1:8011/healthz
curl -s http://127.0.0.1:8011/v1/models

curl -s -X POST http://127.0.0.1:8011/v1/rerank \
  -H 'Content-Type: application/json' \
  -d '{"query":"如何申请退款","documents":["...","..."],"top_n":3}'
```

响应：

```json
{
  "model": "Qwen3-Reranker-0.6B-Base",
  "results": [{"index": 0, "relevance_score": 2.375}, ...],
  "elapsed_ms": 500.9
}
```

`index` 是**输入 documents 数组里的下标**（不是排序后的位置）。空的 query /
documents / documents 里的空字符串会返回 400。

## 运维

```bash
launchctl list | grep rerank
tail -f /tmp/stashbox-rerank-serve.out
launchctl kickstart -k gui/$(id -u)/com.stashbox.rerank-serve
```

环境变量：`RERANK_MODEL` / `RERANK_HOST` / `RERANK_PORT` /
`RERANK_BATCH_SIZE`（默认 16）/ `RERANK_MAX_DOC_LEN`（默认 2048 token）。

## 关于 OCR

OCR **不能**用 mlx-lm（它是纯文本框架，`deepseekocr` / `glm_ocr` 两个架构都不在
里面），但 `mlx-vlm` 0.7.4 都支持（已装进同一个 venv 验证架构解析通过，
内置 8 个 OCR 架构）。注意：那只验证了架构能解析，**没有实测真图识别质量**。

## 内存

模型 4bit 331MB，常驻后端 RSS 约 0.5GB。MLX eval 在同一时刻只允许一个线程
（GPU 串行），和 KnowledgeBase 的 `embedding_server.py` 同一个约定。
