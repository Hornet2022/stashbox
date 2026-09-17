# CP2.7 抓取层集成测（10 真实 URL 端到端）

真网络 + 真 DB 的端到端验证：**服务号 Handler（CP2.5）→ GenericURLFetcher（CP2.4）→ articles 入库**。

## 文件

| 文件 | 作用 |
|---|---|
| `__init__.py` | 包标记（让 pytest 能 collect） |
| `conftest.py` | `db_setup` / `redis_setup` / `content_app` / `ai_client_stub` / `fetch_recorder` fixtures |
| `test_fetch_e2e.py` | 10 个真实 URL parametrized 用例 |
| `README.md` | 本文件 |

## 跑

前置：本机 PostgreSQL 5432 + Redis 6379 在跑（dev 服务 8100-8103 不参与本测）。

```bash
cd /Users/hornet/work/stashbox
PYTHONPATH=/Users/hornet/work:/Users/hornet/work/stashbox \
  backend/.venv/bin/python -m pytest \
  backend/content-service/tests/integration/test_fetch_e2e.py \
  -W ignore::DeprecationWarning -v -s 2>&1 | tee /tmp/cp27_e2e.log
```

期望：`>= 8 passed`、余下 `skipped`（真实网络失败按设计 skip，不是 fail）。

## 报告

每个用例打一行机器可读结果（`-s` 才会落到 stdout）：

```
E2E|url|http_status|title_len|content_text_len|err_code|note
```

解析成 markdown（脚本是临时工具，**不进 git**，放 /tmp）：

```bash
backend/.venv/bin/python /tmp/generate_report.py /tmp/cp27_e2e.log > /tmp/cp27_E2E_REPORT.md
```

`E2E_REPORT.md` **不进 git**（只作任务回执），否则 `git status` 不干净。

## 约定

- **只测通用 fetcher**：不发公众号 / 抖音 URL（那两个 fetcher 仍占位，期望 2001）
- **不改被测代码**：fetcher 抽象层 / Handler 端点保持原样，本目录只做集成验证
- **skip 而非 fail**：超时 / 5xx / 站点限速 / 抽不到正文（2002）都 `pytest.skip`
- **DB 隔离**：Handler 用自己的 session 并 commit，rollback 挡不住 —— `db_setup`
  在 case 结束删掉本 case 时间窗内新增的 `source='wechat_mp'` 行，只碰自己建的行
- **不真打 ai-service**：`ai_client_stub` 替换 `get_ai_client()`，避免污染 distilled_articles
- **正文长度**：`fetch_recorder` 包一层 `GenericURLFetcher.fetch` 记录 `FetchResult`
  （Handler 目前只把 title 落库，正文没进 articles）
