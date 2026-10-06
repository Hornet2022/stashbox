"""后端测试包。

这里必须存在 `__init__.py`，否则 `tests/` 只是 PEP 420 的**命名空间包**。

后果（2026-10 修复）：仓库里有两套同名 tests 目录 ——
`backend/tests/`（本目录，112 个文件）和 `backend/content-service/tests/`
（13 个文件）。加载 `content-service/main.py` 时会把 `content-service/`
插到 sys.path 头部，于是 `content-service/tests` 也以 `tests` 之名可见。

PEP 420 规定：扫描 sys.path 时，遇到没有 `__init__.py` 的同名目录只记为
「命名空间片段」并继续扫；而**正则包一旦找到就立即采用**。
`content-service/tests/integration/` 恰好有 `__init__.py`，`backend/tests/`
原先没有 —— 结果 `from tests.integration.conftest import GATEWAY_URL` 被解析
到 content-service 那一份，ImportError 让整个 pytest 收集失败，
连带 backend 与 android 之外的 1000+ 个用例全部跑不起来。
"""
