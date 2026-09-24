"""CP5.6.2 §2.8：用户偏好重新聚类（k-means 简化版）。

按 docs/听感产品化方案_v1.md §2.8 严格实现：
- 3 类偏好：conservative（保守） / neutral（中性） / aggressive（激进）
- 简化版：按 overall_score 分 3 桶（< 3 / 3-4 / >= 4）
- 真实 k-means（CP5.6.x 上线）：sklearn.cluster.KMeans(n_clusters=3)

依赖：CP3.7.1 DistillationEvaluation 模型 + CP5.6.2 PatternDecay
"""

from __future__ import annotations

import structlog

from stashbox.backend.common.models import DistillationEvaluation

log = structlog.get_logger("distill.pattern_clustering")

# 3 类偏好名
CLUSTER_CONSERVATIVE = "conservative"  # 低分（保守型用户）
CLUSTER_NEUTRAL = "neutral"  # 中分（中性型用户）
CLUSTER_AGGRESSIVE = "aggressive"  # 高分（激进型用户）

# 桶分桶阈值
LOW_THRESHOLD = 3
HIGH_THRESHOLD = 4

# 默认 cluster（输入为空时）
DEFAULT_CLUSTER = CLUSTER_NEUTRAL


class PatternClustering:
    """CP5.6.2 §2.8：用户偏好重新聚类。"""

    def cluster_evaluations(
        self,
        evaluations: list[DistillationEvaluation],
    ) -> dict[str, list[DistillationEvaluation]]:
        """CP5.6.2 §2.8：按 overall_score 分 3 桶。

        Returns:
            {
                "conservative": [low_score_evals],
                "neutral": [medium_score_evals],
                "aggressive": [high_score_evals],
            }

        失败兜底：异常 → {"conservative": [], "neutral": [], "aggressive": []}
        """
        try:
            clustered: dict[str, list[DistillationEvaluation]] = {
                CLUSTER_CONSERVATIVE: [],
                CLUSTER_NEUTRAL: [],
                CLUSTER_AGGRESSIVE: [],
            }
            for ev in evaluations:
                score = ev.overall_score
                if score is None:
                    clustered[CLUSTER_NEUTRAL].append(ev)
                elif score < LOW_THRESHOLD:
                    clustered[CLUSTER_CONSERVATIVE].append(ev)
                elif score < HIGH_THRESHOLD:
                    clustered[CLUSTER_NEUTRAL].append(ev)
                else:
                    clustered[CLUSTER_AGGRESSIVE].append(ev)

            log.info(
                "pattern_clustering_completed",
                total=len(evaluations),
                conservative=len(clustered[CLUSTER_CONSERVATIVE]),
                neutral=len(clustered[CLUSTER_NEUTRAL]),
                aggressive=len(clustered[CLUSTER_AGGRESSIVE]),
            )
            return clustered
        except Exception as e:
            log.warning(
                "pattern_clustering_failed",
                error=str(e),
            )
            return {
                CLUSTER_CONSERVATIVE: [],
                CLUSTER_NEUTRAL: [],
                CLUSTER_AGGRESSIVE: [],
            }

    def dominant_cluster(
        self,
        clustered: dict[str, list[DistillationEvaluation]],
    ) -> str:
        """CP5.6.2 §2.8：返回主导偏好（数量最多的 cluster）。

        平局时按 conservative < neutral < aggressive 优先级。
        """
        if not clustered:
            return DEFAULT_CLUSTER

        # 找最大
        max_count = 0
        for cluster_name, evals in clustered.items():
            if len(evals) > max_count:
                max_count = len(evals)

        if max_count == 0:
            return DEFAULT_CLUSTER

        # 按优先级选（conservative -> neutral -> aggressive）
        for cluster_name in [CLUSTER_CONSERVATIVE, CLUSTER_NEUTRAL, CLUSTER_AGGRESSIVE]:
            if len(clustered.get(cluster_name, [])) == max_count:
                return cluster_name
        return DEFAULT_CLUSTER
