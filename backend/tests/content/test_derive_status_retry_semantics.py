"""蒸馏状态派生契约：失败 ≠ 终态（还在重试时不能显示「失败」）。

## 背景

2026-10-21 内测实测（uid=16）：

    22:16:54  distill_task 开始
    22:17:34  agent_node_failed step=rewrite kind=internal
              error:「LLM 返回空内容（content 为空，重试后仍为空）」
    22:17:49  Arq try=2
    22:18:11  status=done      ← 重试成功（1052 字稿 / 250 秒音频）

用户截到的正是 22:17:34~22:18:11 之间那张「失败 / 换个源试试？点击重新蒸馏」
的页面 —— 而那次重试**其实成功了**。

根因：失败路径无条件把 `articles.status` 标成 failed，而详情页的 status 是
派生的（content-service 的 `_derive_status`：产物表 failed → 返回 failed），
于是「还会自动重试」这件事对用户完全不可见。

## 契约

`distilled_articles.status == 'failed'` 只表示「**上一次**尝试失败了」。
是否终态要看 `articles.status`：

| articles.status | 含义 | 详情页显示 |
|---|---|---|
| pending   | 用户手动点了「重试蒸馏」，等 ai-service 回写 | 处理中 |
| distilling| Arq 还在自动重试 | 蒸馏中 |
| failed    | 最后一次尝试仍失败 | 失败（终态） |
"""

import sys
from pathlib import Path

import pytest

REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _load_derive_status():
    """加载 content-service/main.py 里的 _derive_status（目录名带连字符）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_derive_status_probe", BACKEND_DIR / "content-service" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["_derive_status_probe"] = module
    spec.loader.exec_module(module)
    return module._derive_status


class _FakeArticle:
    def __init__(self, status: str):
        self.status = status


class _FakeTask:
    def __init__(self, status: str | None):
        self.status = status


@pytest.fixture(scope="module")
def derive_status():
    return _load_derive_status()


def test_task_done_is_ready(derive_status):
    """产物表 done → ready（终态）。"""
    assert derive_status(_FakeArticle("distilling"), _FakeTask("done")) == "ready"


def test_final_failure_is_failed(derive_status):
    """最后一次尝试仍失败（articles=failed）→ 失败（终态）。"""
    assert derive_status(_FakeArticle("failed"), _FakeTask("failed")) == "failed"


def test_arq_auto_retry_shows_distilling(derive_status):
    """Arq 还在自动重试 → 显示「蒸馏中」，不是「失败」。

    这就是内测用户截到的那张错误页面 —— 重试其实成功了。
    """
    assert derive_status(_FakeArticle("distilling"), _FakeTask("failed")) == "distilling"


def test_manual_retry_shows_pending(derive_status):
    """用户点了「重试蒸馏」→ 显示处理中，不是「失败」。

    产物表要等 ai-service 回写新任务才更新，在那之前如果还返回 failed，
    用户会以为「重试蒸馏」按钮没生效。
    """
    assert derive_status(_FakeArticle("pending"), _FakeTask("failed")) == "pending"


def test_no_task_falls_back_to_article_status(derive_status):
    """产物表还没建（task=None）→ 用 articles.status。"""
    assert derive_status(_FakeArticle("distilling"), None) == "distilling"


def test_running_task_falls_back_to_article_status(derive_status):
    """产物表 running/queued → 用 articles.status。"""
    assert derive_status(_FakeArticle("distilling"), _FakeTask("running")) == "distilling"
    assert derive_status(_FakeArticle("failed"), _FakeTask("queued")) == "failed"
