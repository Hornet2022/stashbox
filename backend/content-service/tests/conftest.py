"""content-service 测试的全局开关。

## 为什么要在这里关掉浏览器通道

浏览器通道（`fetchers/browser.py`）在**生产默认开启** —— 它是 Tier 2 兜底，
关掉等于剪藏成功率少一档。但测试**绝不能**起真 Chromium：

- 单次 launch 实测 ~1.0s，几十个用例就是几十秒纯等待；
- 更糟的是它会真的去导航测试 URL（`http://test/...`），变成不可控的外网请求。

所以在 conftest（pytest 一定先于测试模块导入）里把环境变量置 0，
让 `browser_tier_enabled()` 读到的就是关闭状态。

这也意味着：**浏览器通道的降级逻辑需要一个不依赖真浏览器的测试替身**，
见 `tests/fetchers/test_pipeline_escalation.py` —— 它用假 channel 验证
"该不该升级"这条决策，而不是靠真浏览器。
"""

from __future__ import annotations

import os

# 退避重试的 sleep 归零：否则每个模拟 5xx 的用例都要真睡 0.6s+
os.environ["STASHBOX_FETCH_RETRY_BASE_DELAY"] = "0"
os.environ["STASHBOX_FETCH_RETRY_ATTEMPTS"] = "3"
# 按 host 节流归零：测试里连续请求同一 host 不该排队
os.environ["STASHBOX_FETCH_HOST_INTERVAL"] = "0"
# 浏览器通道关闭（理由见模块文档）
os.environ.setdefault("STASHBOX_FETCH_BROWSER_TIER", "0")
