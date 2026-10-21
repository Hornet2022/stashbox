"""CP3.8.0 §2.7：TTS 听感盲测（同样文本不同 provider 双盲评分）。

按 docs/听感产品化方案_v1.md §2.7 实现：
- setup_blind_test：生成匿名音频样本 + 隐藏 provider 映射
- compute_blind_score：聚合每个 provider 的中位数

## 2026-10-02：从假 URL 改成真合成

原来 `setup_blind_test` 根本不合成，直接拼一个
``https://tts-blind-test.example/{provider}/{hash}.{i}.m4a`` —— 而端点层又把它
替换成匿名占位 URL。于是评测员建会话、逐条听、逐条打 1-5 分，**揭晓后发现
音频根本放不出来**，全部评分作废。UI 虽标了"模拟数据"，但功能上是废的。

现在走真实 TTS：每个 provider 调 `build_client` 真合成，音频落到
``/tmp/audio/blind-test/``，由 api-gateway 的 `/audio/*` 静态挂载出去
（ENABLE_LOCAL_AUDIO_MOUNT=1 时）。样本 URL 里**不含 provider**，仍然是双盲。

合成本身很贵（本机 mlx-audio 约 24s/38 字），所以：
- 单条文本限制长度（默认截断到 MAX_TEXT_CHARS），避免一次盲测跑几十分钟；
- 合成失败**只让该 provider 的样本不可用**，不影响其它样本；
- 全部失败才抛错。
"""

from __future__ import annotations

import hashlib
import random
import os
import statistics
import structlog
from pathlib import Path
from typing import Any

log = structlog.get_logger("distill.tts_blind_test")

# 盲测文本上限：超出部分截断。合成耗时随字数近似线性上涨
# （实测 38 字 ≈ 24s），不限制的话一次盲测能跑到天亮。
MAX_TEXT_CHARS = 200


# 音频落盘目录。
#
# ⚠️ **不能写死 /tmp**（2026-10-21 修的生产事故）：
#   原来硬编码 `/tmp/audio/blind-test`，注释说"由 api-gateway 的 /audio 挂载"。
#   但那只在**本地开发**成立：
#     · 开发机：网关的 LOCAL_AUDIO_DIR 默认就是 /tmp/audio → 能播
#     · 生产：网关挂的是 LOCAL_AUDIO_DIR（/var/lib/stashbox/data），
#             而且 ai-worker 单元有 PrivateTmp=true —— /tmp 是**私有命名空间**，
#             写进去的��件网关根本看不到。
#   结果：盲测接口返回 200、audio_url 看着像模像样，但运营点"试听"一律 404，
#   整个盲测功能在生产上是废的（而本地测试全绿）。
#
#   现在：从 LOCAL_AUDIO_DIR 派生，跟网关挂载的目录**永远一致**。
#   LOCAL_AUDIO_DIR 本身就是 base_dir（storage key 自带 audio/ 前缀），
#   所以盲测目录是 {LOCAL_AUDIO_DIR}/audio/blind-test。
def _blind_test_dir() -> Path:
    base = os.getenv("LOCAL_AUDIO_DIR", "/tmp/audio")
    return Path(base) / "audio" / "blind-test"


# 保留旧名做兼容（测试与外部引用可能用到）
BLIND_TEST_DIR = _blind_test_dir()


def _public_url_for(path: Path) -> str:
    """把落盘文件路径换算成网关能服务的 URL。

    网关把 ``/audio`` 静态路由挂到 ``LOCAL_AUDIO_DIR``（见
    api-gateway/main.py），所以：

        {LOCAL_AUDIO_DIR}/audio/blind-test/x.wav
        →  /audio/audio/blind-test/x.wav

    写死 "/audio/blind-test/x.wav" 会**少一层** —— 本地开发时
    LOCAL_AUDIO_DIR 默认是 /tmp/audio，落盘也在 /tmp/audio/blind-test/，
    恰好对得上；生产上 LOCAL_AUDIO_DIR 改成别的路径就全错位了。

    从实际路径反推，两边永远一致。
    """
    base = Path(os.getenv("LOCAL_AUDIO_DIR", "/tmp/audio")).resolve()
    try:
        rel = path.resolve().relative_to(base)
    except ValueError:
        # 不在 base 下（配错了）—— 退回按文件名拼，至少能看出路径不对
        log.warning(
            "blind_test_path_outside_base",
            path=str(path),
            base=str(base),
        )
        return f"/audio/{path.name}"
    return "/audio/" + str(rel).replace(os.sep, "/")


class TtsBlindTest:
    """CP3.8.0 §2.7：TTS 盲测。"""

    async def setup_blind_test(
        self,
        text: str,
        providers: list[str],
    ) -> dict[str, Any]:
        """真合成匿名音频样本 + 随机展示序。

        Returns:
            {
                "sample_1": "<可播放 URL>",
                "sample_2": "<可播放 URL>",
                "order": ["provider_a", "provider_b"],  # 隐藏映射（展示序）
                "failed": ["provider_c"],              # 合成失败的 provider
            }

        失败兑底：全部 provider 都失败 → samples 为空，调用方据此报错。
        """
        try:
            body = (text or "").strip()[:MAX_TEXT_CHARS]
            if not body:
                return {"samples": {}, "order": [], "failed": list(providers)}

            # 每次现取目录（不缓存成模块常量）—— env 改了要立刻生效，
            # 且与网关的 LOCAL_AUDIO_DIR 保持同步
            blind_dir = _blind_test_dir()
            blind_dir.mkdir(parents=True, exist_ok=True)
            # 用文本+provider 的哈希做文件名：同一文本重复盲测不堆积文件，
            # 且文件名不含 provider 信息（双盲的前提）
            token = hashlib.sha256(body.encode()).hexdigest()[:12]

            samples: dict[str, str] = {}
            failed: list[str] = []
            ok_providers: list[str] = []

            for i, provider in enumerate(providers):
                sample_key = f"sample_{i + 1}"
                try:
                    path = await self._synthesize_to_file(body, provider, token, i)
                    # URL 从**实际落盘路径**反推，而不是写死 "/audio/blind-test/xxx"。
                    # 写死会跟网关的挂载目录错位（网关挂的是 LOCAL_AUDIO_DIR，
                    # 实际文件在其下的 audio/ 子目录里）—— 表现是接口 200、
                    # audio_url 看着正常，但点"试听"一律 404。
                    samples[sample_key] = _public_url_for(path)
                    ok_providers.append(provider)
                except Exception as exc:
                    log.warning(
                        "blind_test_synth_failed",
                        provider=provider,
                        error=str(exc),
                    )
                    failed.append(provider)

            if not samples:
                log.error("blind_test_all_failed", providers=providers)
                return {"samples": {}, "order": [], "failed": failed}

            # 随机打乱展示顺序（让评测员不能猜出 provider）
            shuffled = ok_providers.copy()
            random.shuffle(shuffled)

            return {"samples": samples, "order": shuffled, "failed": failed}
        except Exception as exc:
            log.warning("blind_test_setup_failed", error=str(exc))
            return {"samples": {}, "order": [], "failed": list(providers)}

    async def _synthesize_to_file(
        self,
        text: str,
        provider: str,
        token: str,
        index: int,
    ) -> Path:
        """真合成一段音频并落盘，返回文件路径。"""
        from stashbox.backend.app.services.tts import build_client

        client = build_client({"provider": provider})
        audio = await client.synthesize(text, output_format="wav")
        if not audio:
            raise RuntimeError(f"{provider} 合成返回空音频")

        out = _blind_test_dir() / f"{token}_{index}.wav"
        out.write_bytes(audio)
        log.info("blind_test_synth_ok", provider=provider, path=str(out), bytes=len(audio))
        return out

    def compute_blind_score(
        self,
        evaluator_scores: dict[str, list[float]],
        provider_mapping: dict[str, str],
    ) -> dict[str, float]:
        """聚合盲测分：每个 provider 的中位数。

        用中位数而不是均值：听感分是个人的、有离群值，均值容易被单个极端
        评分拉偏。

        Args:
            evaluator_scores: {sample_key: [score1, score2, ...]}
            provider_mapping: {sample_key: "provider_name"}

        Returns:
            {provider_name: median_score}

        失败兑底：异常 → return {}
        """
        try:
            provider_scores: dict[str, list[float]] = {}
            for sample_key, scores in evaluator_scores.items():
                provider = provider_mapping.get(sample_key)
                if provider is None:
                    continue
                provider_scores.setdefault(provider, []).extend(scores)

            result = {
                provider: statistics.median(scores)
                for provider, scores in provider_scores.items()
                if scores
            }
            log.info("tts_blind_score_computed", num_providers=len(result))
            return result
        except Exception as e:
            log.warning("tts_blind_score_failed", error=str(e))
            return {}
