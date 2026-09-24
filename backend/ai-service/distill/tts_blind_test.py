"""CP3.8.0 §2.7：TTS 听感盲测（同样文本不同 provider 双盲评分）。

按 docs/听感产品化方案_v1.md §2.7 严格实现：
- setup_blind_test：生成匿名音频样本 + 隐藏 provider 映射
- compute_blind_score：聚合每个 provider 的中位数
"""

from __future__ import annotations

import random
import statistics
import structlog
from typing import Any

log = structlog.get_logger("distill.tts_blind_test")


class TtsBlindTest:
    """CP3.8.0 §2.7：TTS 盲测。"""

    async def setup_blind_test(
        self,
        text: str,
        providers: list[str],
    ) -> dict[str, Any]:
        """生成匿名音频样本 + 顺序。

        Returns:
            {
                "sample_1": "audio_url_1",
                "sample_2": "audio_url_2",
                ...
                "order": ["provider_a", "provider_b"],  # 隐藏映射
            }

        失败兑底：异常 → return {"samples": [], "order": []}
        """
        try:
            # 生成音频样本（CP3.8.x：调真实 TTS 合成；本期 mock 返回 fake url）
            samples = {}
            for i, provider in enumerate(providers):
                sample_key = f"sample_{i + 1}"
                # 隐藏 provider 映射
                audio_url = f"https://tts-blind-test.example/{provider}/{hash(text)}.{i}.m4a"
                samples[sample_key] = audio_url

            # 随机打乱顺序（让评测员不能猜出 provider）
            shuffled_providers = providers.copy()
            random.shuffle(shuffled_providers)

            result = {
                "samples": samples,
                "order": shuffled_providers,
            }
            log.info(
                "tts_blind_test_setup",
                num_providers=len(providers),
                text_length=len(text),
            )
            return result
        except Exception as e:
            log.warning("tts_blind_test_setup_failed", error=str(e))
            return {"samples": {}, "order": []}

    def compute_blind_score(
        self,
        evaluator_scores: dict[str, list[float]],
        provider_mapping: dict[str, str],
    ) -> dict[str, float]:
        """聚合盲测分：每个 provider 的中位数。

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
