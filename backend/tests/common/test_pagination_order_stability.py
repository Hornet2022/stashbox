"""分页排序必须带**唯一 tiebreaker**（静态契约，2026-10-22）。

⚠️ **为什么是静态检查，而不是行为测试**：

「同一个用户出现在两页、另一个人两页都看不到」这个 bug 的机制是：
排序只按 `created_at DESC`，而 PostgreSQL 对**并列**元素的顺序取决于
**执行计划**（top-N heapsort / index scan / seq+sort 各有各的并列处理）。
同一个查询在同一个计划下结果是稳定的 → 两次翻页拿到不同的计划
（比如第一页走索引、第二页因为 OFFSET 变了换了计划）→ 并列部分顺序翻转
→ 重复 + 漏行。

于是：
  · 行为测试**复现不稳定**（实测：加了断言、撤掉修复，测试照样绿）
  · 静态检查**确定性地**守住不变量：「凡是用 OFFSET/LIMIT 翻页的查询，
    排序里必须有一个唯一列（主键）做 tiebreaker」

实测踩过的坑（2026-10-22，全量测试随机翻红）：
    tests/admin/test_admin_users.py::test_list_users_pagination
    assert not ({46762} & {46762})   ← 同一个用户同时出现在第 1、2 页
根因：user-service 的用户列表只按 created_at DESC 排，没有 id 兜底。
"""

import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]

#: 会做 OFFSET/LIMIT 翻页的服务文件（新增分页时记得加进来）
PAGED_FILES = [
    "user-service/main.py",
    "content-service/main.py",
    "content-service/admin_router.py",
    "ai-service/admin_router.py",
    "ai-service/tasks/distill_task.py",
]


def _order_by_of_offset(node: ast.AST) -> ast.Call | None:
    """从 `.offset(...)` 这个调用往上找同一串链式调用里的 `.order_by(...)`。"""
    cur = node
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
        if cur.func.attr == "order_by":
            return cur
        cur = cur.func.value
    return None


def _has_unique_tiebreaker(order_by_call: ast.Call) -> bool:
    """排序里有没有一个「唯一列」（主键 id / 复合主键的任一列）。"""
    for arg in order_by_call.args:
        for node in ast.walk(arg):
            if isinstance(node, ast.Attribute) and node.attr == "id":
                return True
            # desc("id") / asc("id") 这种字符串写法
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value == "id" or node.value.endswith(".id"):
                    return True
    return False


def _iter_offset_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "offset":
                yield node


def test_分页排序必须带唯一tiebreaker():
    """所有 OFFSET 翻页的查询，排序里必须有唯一列兜底。"""
    problems: list[str] = []
    checked = 0

    for rel in PAGED_FILES:
        path = BACKEND_DIR / rel
        assert path.exists(), f"文件不存在：{rel}"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for offset_call in _iter_offset_calls(tree):
            order_by = _order_by_of_offset(offset_call)
            if order_by is None:
                continue  # 没显式排序 → 由 DB 自己排，不属于本契约
            checked += 1
            if not _has_unique_tiebreaker(order_by):
                src = ast.unparse(order_by)
                problems.append(f"{rel}: order_by({src}) 没有唯一 tiebreaker")

    assert checked > 0, "一个带 offset 的分页查询都没扫到 —— 检查器本身失效了"
    assert not problems, "分页排序缺唯一 tiebreaker（会导致翻页重复/漏行）：\n" + "\n".join(
        problems
    )


def test_已知修好的地方仍然带着tiebreaker():
    """钉住具体位置：这些是 2026-10-22 修过的，别被改回去。"""
    cases = {
        "user-service/main.py": "User.created_at.desc(), User.id.desc()",
        "content-service/admin_router.py": "Article.created_at.desc(), Article.id.desc()",
        "ai-service/admin_router.py": "FewShotExample.score_avg.desc(), FewShotExample.id.desc()",
    }
    for rel, needle in cases.items():
        src = (BACKEND_DIR / rel).read_text(encoding="utf-8")
        assert needle in src, f"{rel} 里找不到「{needle}」—— 分页 tiebreaker 被改回去了？"
