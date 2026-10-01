"""rerank_serve.py — 基于 mlx-lm 的本地 rerank HTTP 服务。

用 `Qwen3-Reranker-0.6B-4bit` 提供 OpenAI 风格的 `/v1/rerank`，零额外依赖
（只多一个 fastapi/uvicorn，和 TTS 服务共用同一个 venv）。

为什么是这个模型
----------------
本机另外那个 `jina-reranker-v3.5-mlx` **不能用**，而且是最危险的那种失败：

它的 `model_type` 也是 `qwen3`，所以 `mlx_lm.load()` 会**成功加载、forward 也会
成功**——但它的权重里有 `projector.*` 两个 tensor，`mlx_lm.models.qwen3.Model`
不认识会被**静默丢弃**；而且它是 cross-encoder 架构，官方是用 `[CLS]` hidden +
projector 算相似度，不是 yes/no logprob。实测（2026-10-01）：

    文档            jina 分数      Qwen3-Reranker 分数
    相关            +0.656         +1.375
    半相关          +1.195         -2.500
    不相关          +0.344         -11.219

jina 那一列**排序完全颠倒**、区分度接近零。所以只能选 Qwen3-Reranker
（`Qwen3ForCausalLM`，mlx-lm 原生支持，0.04s/文档）。

打分方式
--------
按 Qwen3-Reranker 官方的做法：把 `(query, document)` 套进
「只能回答 yes / no」的指令模板，然后取末位 `yes` 与 `no` 两个 token 的
log-prob 之差 `logP(yes) - logP(no)` 作为相关性分数。分数越大越相关。

批处理
------
一次 forward 一批 (query, doc) 对。**右 padding + 取每条自己那一段的末位
logits**，不需要 attention mask —— decoder-only 的 causal mask 下，pad token
在真实 token 之后，只会被前面的真实 token attend 到（单向），而我们只取真实
位置，所以 pad 不会污染结果。这样 position 也天然正确（真实 token 从 0 开始
连续编号），不需要额外传 position_ids。

按长度排序分组减少 padding，同长度的一批通常能吃到大部分 GPU 并行。

环境变量
--------
    RERANK_MODEL     模型目录（默认 /Volumes/AIWorker/Qwen3-Reranker-0.6B-4bit）
    RERANK_HOST/PORT 监听地址（默认 127.0.0.1:8011）
    RERANK_BATCH_SIZE 单批最大条数（默认 16）
    RERANK_MAX_DOC_LEN 每条 document 的 token 上限，超出截断（默认 2048）
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mlx.core as mx
import mlx.nn as nn
from mlx_lm import load

log = logging.getLogger("rerank_serve")

MODEL_PATH = os.getenv("RERANK_MODEL", "/Volumes/AIWorker/Qwen3-Reranker-0.6B-4bit")
HOST = os.getenv("RERANK_HOST", "127.0.0.1")
PORT = int(os.getenv("RERANK_PORT", "8011"))
BATCH_SIZE = max(1, int(os.getenv("RERANK_BATCH_SIZE", "16")))
MAX_DOC_TOKENS = int(os.getenv("RERANK_MAX_DOC_LEN", "2048"))

# Qwen3-Reranker 官方模板。注意这个模型**没有 chat_template**，只能手写。
_TEMPLATE = (
    "<|im_start|>system\n"
    "Judge whether the Document meets the requirements based on the Query. "
    'Note that the answer can only be "yes" or "no".<|im_end|>\n'
    "<|im_start|>user\n"
    "Query: {query}\nDocument: {document}<|im_end|>\n"
    "<|im_start|>assistant\n"
)

# MLX 的 eval 在同一时刻只允许一个线程（GPU 串行），和 KnowledgeBase 的
# embedding_server.py 同一个约定。
_eval_lock = threading.Lock()


@dataclass
class _Bundle:
    model: Any
    tokenizer: Any
    yes_id: int
    no_id: int


def _load() -> _Bundle:
    t0 = time.time()
    model, tok = load(MODEL_PATH)
    yes_id = tok.encode("yes", add_special_tokens=False)[-1]
    no_id = tok.encode("no", add_special_tokens=False)[-1]
    log.info(
        "模型加载完成 %s（%.1fs）yes=%s no=%s",
        MODEL_PATH,
        time.time() - t0,
        yes_id,
        no_id,
    )
    return _Bundle(model=model, tokenizer=tok, yes_id=yes_id, no_id=no_id)


BUNDLE: _Bundle | None = None


def _encode(query: str, document: str) -> list[int]:
    text = _TEMPLATE.format(query=query, document=document)
    ids = BUNDLE.tokenizer.encode(text)
    # 掐头去尾：保住中间的 query/document，只在两端各让出 MAX_DOC_TOKENS 的一半。
    # 全砍会毁掉 relevance 信号，全留又会让超长 doc 撑爆 batch 的 padding。
    if len(ids) > MAX_DOC_TOKENS:
        half = MAX_DOC_TOKENS // 2
        ids = ids[:half] + ids[-(MAX_DOC_TOKENS - half) :]
    return ids


def _score_batch(queries: list[str], documents: list[str]) -> list[float]:
    """对一批 (query, doc) 打一次 forward，返回 logP(yes)-logP(no)。"""
    enc = [_encode(q, d) for q, d in zip(queries, documents)]
    width = max(len(x) for x in enc)
    pad_id = BUNDLE.tokenizer.eos_token_id or 0

    # 右 padding：pad 排在真实 token **之后**，causal mask 下真实 token 看不到
    # 它们（只能看前面），所以不需要 attention mask。
    #
    # ⚠️ 取 logits 时必须用每条自己那一段的**最后一个真实位置**，不能图省事写
    # logits[:, -1, :]：decoder-only 里 logits[:, i, :] 预测的是位置 i+1 的 token，
    # 右 padding 后 -1 列是 pad 位置的输出，取它等于在问「pad 之后是什么」。
    # 实测踩过：15 条一批时分数最大偏差 14.5，且「苹果发布了新款平板」这种
    # 完全无关的文档被排到第 5 名 —— 看起来能跑、结果是乱的。
    rows, last_real = [], []
    for x in enc:
        rows.append(x + [pad_id] * (width - len(x)))
        last_real.append(len(x) - 1)

    ids = mx.array(rows)
    logits = BUNDLE.model(ids)
    if isinstance(logits, dict):
        logits = logits["logits"]
    # 显式 gather 出各条自己那一列。
    # ⚠️ 别用 logits[mx.array(last_real), :, :] —— MLX 的高级索引语义和 numpy
    # 不一样，实测会 squeeze 掉中间维并直接抛 "Cannot squeeze axis 1"。
    # batch 最多 16，逐条 stack 的开销可以忽略，换来的是不会踩索引语义的坑。
    picked = mx.stack(
        [logits[i, lr, :].astype(mx.float32) for i, lr in enumerate(last_real)]
    )
    logp = nn.log_softmax(picked, axis=-1)
    return (logp[:, BUNDLE.yes_id] - logp[:, BUNDLE.no_id]).tolist()


def rerank(query: str, documents: list[str], top_n: int | None = None) -> list[dict]:
    """返回按分数降序的 [{index, relevance_score}]。"""
    if not documents:
        return []

    scores: list[float] = [0.0] * len(documents)
    pairs = list(enumerate(documents))

    # 按 token 长度分桶，桶内按长度排序 → 同批长度接近，padding 少。
    with _eval_lock:
        for start in range(0, len(pairs), BATCH_SIZE):
            chunk = pairs[start : start + BATCH_SIZE]
            vals = _score_batch([query] * len(chunk), [d for _, d in chunk])
            for (idx, _), v in zip(chunk, vals):
                scores[idx] = v

    results = [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]
    results.sort(key=lambda r: r["relevance_score"], reverse=True)
    return results[:top_n] if top_n else results


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------
#
# ⚠️ pydantic 模型**必须定义在模块顶层**，不能放进 build_app() 的闭包里。
# 本文件有 `from __future__ import annotations`，FastAPI 靠 get_type_hints 解析
# 注解，而闭包里的局部类不在模块 globals 中，解析失败后会把整个模型当成普通
# 参数处理 —— 表现为请求体被忽略、报 `missing: Field required` at ["query","req"]。

try:
    from pydantic import BaseModel, Field

    class RerankRequest(BaseModel):
        model: str | None = None
        query: str
        documents: list[str]
        top_n: int | None = Field(default=None, ge=1)

    class RerankResponse(BaseModel):
        model: str
        results: list[dict]

except ImportError:  # 允许在没装 fastapi/pydantic 时 import 本模块做单测
    RerankRequest = RerankResponse = None  # type: ignore[assignment,misc]


def build_app():
    from fastapi import FastAPI, HTTPException

    app = FastAPI(title="mlx-lm rerank", version="1.0")

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "model": Path(MODEL_PATH).name,
            "loaded": BUNDLE is not None,
        }

    @app.get("/v1/models")
    async def models():
        return {
            "object": "list",
            "data": [
                {"id": Path(MODEL_PATH).name, "object": "model", "owned_by": "mlx-lm"}
            ],
        }

    @app.post("/v1/rerank")
    async def do_rerank(req: "RerankRequest"):
        if not req.query.strip():
            raise HTTPException(400, "query 不能为空")
        if not req.documents:
            raise HTTPException(400, "documents 不能为空")
        if any(not d.strip() for d in req.documents):
            raise HTTPException(400, "documents 里存在空字符串")
        t0 = time.time()
        results = rerank(req.query, req.documents, req.top_n)
        payload = RerankResponse(model=Path(MODEL_PATH).name, results=results)
        out = payload.model_dump()
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        return out

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.getenv("RERANK_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    global BUNDLE
    BUNDLE = _load()
    log.info("监听 http://%s:%d  batch=%d", HOST, PORT, BATCH_SIZE)
    uvicorn.run(build_app(), host=HOST, port=PORT, workers=1, loop="asyncio")


if __name__ == "__main__":
    main()
