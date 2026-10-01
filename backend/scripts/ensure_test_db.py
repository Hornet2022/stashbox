#!/usr/bin/env python3
"""ensure_test_db.py — 建/重建测试库 `stashbox_test`，并跑真实 alembic 迁移。

为什么要单独一个脚本而不是塞进 conftest fixture：
业务模块（common.config / common.database）在 **import 期**就读配置建连接池，
所以 conftest 必须在模块顶层改 os.environ（见 tests/conftest.py 顶部）。而建库
本身是「一次性准备工作」，塞进 session fixture 会让它的 stdout/失败原因被
pytest 的输出吞掉。拆成脚本后可以单独跑、单独看报错。

做法：
  1. 连 postgres 维护库（postgres）确认目标库不存在就 CREATE
  2. 用 alembic upgrade head 把表建齐
     —— 刻意**不**用 metadata.create_all：迁移文件里有 ALTER / 索引 /
        数据回填等 create_all 表达不了的东西，create_all 建出来的 schema 与生产
        不一致，测出来的东西没有意义。

幂等：库和表都已到位时直接跳过，不做任何写入。

用法：
    python scripts/ensure_test_db.py          # 确保存在
    python scripts/ensure_test_db.py --drop   # 先删库重建（怀疑 schema 漂移时）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_PARENT = BACKEND_DIR.parents[1]

TEST_DB = os.getenv("POSTGRES_DB", "stashbox_test")
PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
PG_USER = os.getenv("POSTGRES_USER", "stashbox")
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD", "stashbox_dev")

# 保护：绝不允许把生产库当测试库删掉。
FORBIDDEN = {"stashbox", "postgres", "template0", "template1"}


def _psql(sql: str, dbname: str) -> str:
    """跑一条 SQL 并返回 stdout；失败抛 RuntimeError。"""
    env = {**os.environ, "PGPASSWORD": PG_PASSWORD}
    proc = subprocess.run(
        [
            "psql",
            "-h",
            PG_HOST,
            "-p",
            str(PG_PORT),
            "-U",
            PG_USER,
            "-d",
            dbname,
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql 失败 ({dbname}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drop", action="store_true", help="先删库再重建")
    args = parser.parse_args()

    if TEST_DB in FORBIDDEN:
        print(f"✗ 拒绝操作：{TEST_DB} 看起来是真实业务库", file=sys.stderr)
        return 2

    if args.drop:
        print(f"▶ 重建 {TEST_DB}（--drop）")
        try:
            _psql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{TEST_DB}' AND pid<>pg_backend_pid()",
                "postgres",
            )
            _psql(f'DROP DATABASE IF EXISTS "{TEST_DB}"', "postgres")
        except RuntimeError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1

    exists = _psql(f"SELECT 1 FROM pg_database WHERE datname='{TEST_DB}'", "postgres")
    if not exists:
        print(f"▶ 创建 {TEST_DB}")
        try:
            _psql(f'CREATE DATABASE "{TEST_DB}" OWNER "{PG_USER}"', "postgres")
        except RuntimeError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
    else:
        print(f"• {TEST_DB} 已存在")

    # alembic 用同一套 env（POSTGRES_DB 已经是测试库）
    env = {
        **os.environ,
        "POSTGRES_DB": TEST_DB,
        "POSTGRES_HOST": PG_HOST,
        "POSTGRES_PORT": str(PG_PORT),
        "POSTGRES_USER": PG_USER,
        "POSTGRES_PASSWORD": PG_PASSWORD,
        "PYTHONPATH": str(REPO_PARENT),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        print("✗ alembic upgrade head 失败", file=sys.stderr)
        print(proc.stdout[-3000:], file=sys.stderr)
        print(proc.stderr[-3000:], file=sys.stderr)
        return 1

    tables = _psql(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'",
        TEST_DB,
    )
    print(f"✅ {TEST_DB} 就绪（{tables} 张表）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
