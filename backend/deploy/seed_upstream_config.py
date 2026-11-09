#!/usr/bin/env python3
"""把 env 里的上游身份**一次性播种**进 DB（`system_config` 表）。

## 为什么需要这个脚本

2026-11-10 起，上游（LLM / TTS）配置**只认 DB** —— 运行时彻底不再读 env
（见 `backend/common/upstream_config.py` 的契约）。

但**存量部署**的 env 里已经有配好的上游身份（base_url / api_key / model）。
直接删掉 env 里的这些变量会让新部署**没有配置可用** ⇒ 蒸馏全线不可用。

所以流程是：
    ① 用本脚本把 env 里的上游身份**写进 DB**（一次）；
    ② 确认 DB 里生效（`--verify`）；
    ③ 之后 env 里的上游身份变量就可以**删掉**了。

## 关键约定

- **只播种，不覆盖**：默认只在 DB 里**没有**该字段时才写入。
  DB 里已有的值**一律不动**（DB 才是真相源，env 只是补种）。
  要强制覆盖用 `--force`。
- **不读 .env 文件**（避免和生产的 `/etc/stashbox/stashbox.env` 语义混淆）：
  默认读**当前进程的环境变量**。要指定 env 文件用 `--env-file`。
- env 文件的解析**不用 `source`**（生产 env 文件含中文注释，`source` 会报
  `command not found`）—— 逐行读 `key=value`，过滤 `#` 注释与非法键名。

## 用法

    # 从当前环境播种（DB 里已有的字段不动）
    python backend/deploy/seed_upstream_config.py

    # 从指定 env 文件播种，并强制覆盖 DB
    python backend/deploy/seed_upstream_config.py --env-file /etc/stashbox/stashbox.env --force

    # 只看会写什么，不落库
    python backend/deploy/seed_upstream_config.py --env-file /etc/stashbox/stashbox.env --dry-run

    # 只检查 DB 里现在配全了没有（不改任何东西）
    python backend/deploy/seed_upstream_config.py --verify
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

# `stashbox.backend.*` 是绝对导入 ⇒ 需要仓库的**父目录**在 sys.path 上
# （生产是 `/opt/stashbox`，本机是 `/Users/hornet/work`）。
_REPO_PARENT = str(Path(__file__).resolve().parents[3])
if _REPO_PARENT not in sys.path:
    sys.path.insert(0, _REPO_PARENT)


# ⚠️ `system_config` **不能**在模块顶层 import：
#    它的导入链是 system_config → common.database → common.config → settings，
#    而 settings 在**导入时**就校验 JWT_SECRET 等生产必填项 ——
#    那时 env 文件还没被灌进来（--env-file 是在 main() 里才 parse 的），
#    于是脚本会在 import 阶段就崩，连"能不能播种"都来不及判断。
#    ⇒ 改成**懒导入**：先把 env 灌进 os.environ，再 import。
def _keys() -> tuple[str, str]:
    """懒导入 system_config 的两个 key 常量（此时 env 已就绪）。"""
    from stashbox.backend.common.system_config import KEY_LLM, KEY_TTS

    return KEY_LLM, KEY_TTS


# ── env → DB 字段的映射 ─────────────────────────────────────────────────
#
# 只列**身份类**字段（决定"打哪个上游 / 用什么凭据 / 用哪个模型 / 什么音色"）。
# 传输类参数（分块长度、并发度、超时…）**不播种** —— 它们本来就留在 env。
LLM_ENV_MAP: dict[str, str] = {
    "LLM_PROVIDER": "provider",
    "OPENAI_LLM_MODEL": "model",
    "OPENAI_LLM_API_KEY": "api_key",
    "OPENAI_LLM_BASE_URL": "base_url",
    "QWEN_VL_MODEL": "model",
    "QWEN_VL_API_KEY": "api_key",
    "QWEN_VL_BASE_URL": "base_url",
    "DASHSCOPE_API_KEY": "api_key",
    # 调用预算（2026-11-10 起纳入 DB）
    "LLM_TIMEOUT": "timeout",
    "LLM_MAX_RETRIES": "max_retries",
    "LLM_CONNECT_TIMEOUT": "connect_timeout",
}

TTS_ENV_MAP: dict[str, str] = {
    "TTS_PROVIDER": "provider",
    "OPENAI_TTS_API_KEY": "openai_api_key",
    "OPENAI_BASE_URL": "openai_base_url",
    "OPENAI_TTS_MODEL": "openai_model",
    "OPENAI_TTS_VOICE": "openai_voice",
    "OPENAI_TTS_INSTRUCTIONS": "openai_instructions",
    "OPENAI_TTS_LANGUAGE": "openai_language",
    "DOUBAO_TTS_API_KEY": "doubao_api_key",
    "DOUBAO_TTS_TOKEN": "doubao_token",
    "DOUBAO_TTS_APP_ID": "doubao_app_id",
    "DOUBAO_TTS_VOICE": "doubao_voice",
    "DOUBAO_TTS_RESOURCE_ID": "doubao_resource_id",
    "EDGE_TTS_VOICE": "edge_voice",
    "TTS_LOCAL_VOICE": "local_voice",
    "INDEXTTS_BASE_URL": "indextts_base_url",
    "INDEXTTS_MODEL": "indextts_model",
    "INDEXTTS_REF_AUDIO": "indextts_ref_audio",
    "INDEXTTS_REF_TEXT": "indextts_ref_text",
    "INDEXTTS_SPEAKER": "indextts_speaker",
    "INDEXTTS_INSTRUCT": "indextts_instruct",
}

# 数值型字段（要转成数字再落库，否则 DB 里存的是字符串，运行时比较会出错）
_NUMERIC_FIELDS = {
    "timeout",
    "max_retries",
    "connect_timeout",
    "indextts_temperature",
    "indextts_top_p",
}


def parse_env_file(path: str) -> dict[str, str]:
    """读 env 文件 → dict。

    **不用 `source`**：生产的 env 文件含中文注释，`source` 会把注释当命令
    执行 → `command not found`。这里逐行解析：只认 `KEY=VALUE`，
    跳过空行、`#` 开头、以及键名不合法的行。
    """
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # 合法的 env 键名：字母/数字/下划线，且不以数字开头
            if not key or key[0].isdigit() or not all(c.isalnum() or c == "_" for c in key):
                continue
            value = value.strip()
            # 去掉成对的引号
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key] = value
    return out


def collect(env: dict[str, str], mapping: dict[str, str]) -> dict[str, Any]:
    """按映射从 env 里挑出要播种的字段。

    同名目标字段（如 `model` 既可能来自 `OPENAI_LLM_MODEL` 也可能来自
    `QWEN_VL_MODEL`）按**映射表的先后**取第一个非空值。
    """
    out: dict[str, Any] = {}
    for env_name, field in mapping.items():
        raw = env.get(env_name)
        if raw is None or raw == "":
            continue
        if field in out:
            continue
        if field in _NUMERIC_FIELDS:
            try:
                out[field] = int(raw) if field == "max_retries" else float(raw)
            except ValueError:
                print(f"  ! {env_name}={raw!r} 不是数字，跳过", file=sys.stderr)
                continue
        else:
            out[field] = raw
    return out


async def seed(key: str, fields: dict[str, Any], *, force: bool, dry_run: bool) -> int:
    """把 fields 合并进 system_config 的 key 行。返回改动字段数。"""
    from stashbox.backend.common.system_config import get_config, set_config

    stored = dict(await get_config(key) or {})
    merged = dict(stored)
    changed = 0
    for field, value in fields.items():
        if not force and stored.get(field) not in (None, ""):
            continue  # DB 里已有 ⇒ 不动（DB 才是真相源）
        merged[field] = value
        changed += 1

    if changed == 0:
        print(f"[{key}] 无需改动（DB 里已有这些字段）")
        return 0
    if dry_run:
        print(f"[{key}] dry-run：将会写入 {changed} 个字段：")
        for field, value in merged.items():
            mark = "*" if stored.get(field) in (None, "") else " "
            shown = "***" if "key" in field or "token" in field else value
            print(f"   {mark} {field} = {shown}")
        return changed

    await set_config(key, merged)
    print(f"[{key}] 已写入 {changed} 个字段（force={force}）")
    return changed


async def verify() -> int:
    """只读检查：DB 里两组上游配置配全了没有。返回问题数。"""
    from stashbox.backend.common.upstream_config import (
        UpstreamConfigUnavailable,
        diagnose,
    )

    try:
        report = await diagnose()
    except UpstreamConfigUnavailable as exc:
        print(f"! 上游配置读不到：{exc}")
        return 1

    problems = 0
    for name, info in report.items():
        if info.get("ok"):
            print(
                f"[{name}] OK  provider={info.get('provider')} "
                f"model={info.get('model')} base_url={info.get('base_url')} "
                f"source={info.get('source')}"
            )
        else:
            problems += 1
            print(f"[{name}] 不完整：missing={info.get('missing')} source={info.get('source')}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="把 env 里的上游身份一次性播种进 DB")
    parser.add_argument("--env-file", help="env 文件路径（不指定则用当前进程的环境变量）")
    parser.add_argument("--force", action="store_true", help="覆盖 DB 里已有的值")
    parser.add_argument("--dry-run", action="store_true", help="只打印会写什么，不落库")
    parser.add_argument("--verify", action="store_true", help="只检查 DB 配置，不改任何东西")
    args = parser.parse_args()

    # ⚠️ **必须先灌 env，再 import 任何碰 DB 的模块**：
    #    system_config → common.database → common.config → settings，
    #    而 settings 在**导入时**就校验生产必填项（JWT_SECRET 等）。
    #    env 文件还没灌进去就 import ⇒ 脚本在 import 阶段就崩。
    env: dict[str, str] = dict(os.environ)
    if args.env_file:
        parsed = parse_env_file(args.env_file)
        os.environ.update(parsed)
        env.update(parsed)

    if args.verify:
        return 1 if asyncio.run(verify()) else 0

    key_llm, key_tts = _keys()

    llm_fields = collect(env, LLM_ENV_MAP)
    tts_fields = collect(env, TTS_ENV_MAP)

    if not llm_fields and not tts_fields:
        print("env 里没找到任何上游身份变量（对照 LLM_ENV_MAP / TTS_ENV_MAP）")
        return 1

    print(f"从 env 读到：llm {sorted(llm_fields)} / tts {sorted(tts_fields)}")

    changed = 0
    if llm_fields:
        changed += asyncio.run(seed(key_llm, llm_fields, force=args.force, dry_run=args.dry_run))
    if tts_fields:
        changed += asyncio.run(seed(key_tts, tts_fields, force=args.force, dry_run=args.dry_run))

    if args.dry_run:
        print(f"（dry-run：共 {changed} 个字段会被写入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
