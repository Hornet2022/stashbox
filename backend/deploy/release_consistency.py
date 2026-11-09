#!/usr/bin/env python3
"""「当前版本」三处真相源的一致性判定（纯函数，可测）。

## 为什么有这一个模块

「当前版本」这件事有**三处**记录：

    ① 服务端 env 的 STASHBOX_APP_* 变量（版本接口读它）
    ② /downloads/latest.apk 软链（营销页下载入口指向它）
    ③ 实际文件 /var/www/stashbox-downloads/tingxia-<ver>.apk

三处各说各的时，**没有任何一处会报错** —— 页面上看起来一切正常。

2026-11-09 实测踩的就是这个：手工分三步发版（scp → 软链 → env），
**漏了 env 那一步** ⇒

    · 服务器上有 0.5.2 的包，软链也指向它（③ ✓ ② ✓）
    · 但 env 还停在 0.5.1 ⇒ 版本接口返回 code=24（① ✗）
    · 安卓客户端判 `latest > installed` 为假 ⇒ 显示"已是最新版本"
    · ⇒ **根本没有下载入口** ⇒ 内测用户反馈"无法下载新版本"

症状离原因隔了三层，排查时很容易怀疑"下载挂了""包坏了""网络问题" —— 全都不是。

## 为什么判定逻辑要独立成模块

`verify_release_consistency.sh` 里的判定一开始是内嵌 heredoc 的 python。
问题是：**内嵌的代码没法被测试直接覆盖** ⇒ 每条判据都得靠"人工跑一次看它报不报"
来验证 ⇒ 而"报不报错"本身又依赖生产环境的状态 ⇒ 护栏没有牙齿。

抽成模块后：
  · 判据只有**一份实现**（脚本与测试共用）
  · 每条判据都能用**构造出来的数据**单独验证
  · 测试不碰网络、不碰生产

## 判据清单（每条都要能被单独测出来）

| # | 判据 | 漏了会怎样 |
|---|---|---|
| 1 | env 的 versionCode == 版本接口的 versionCode | user-service 没重启 ⇒ 接口说旧号 |
| 2 | env 的版本名 == 版本接口的版本名 | 同上 |
| 3 | env 的下载地址 == 版本接口的下载地址 | 同上 |
| 4 | 版本接口下发的包名 == 软链指向的文件名 | 用户点更新拿到跟营销页不一样的包 |
| 5 | 软链指向的文件在磁盘上 | 下载 404 |
| 6 | 软链名里的版本 == env 的版本名 | **2026-11-09 踩的那个坑**（包在、软链对、但接口说旧号） |
| 7 | versionCode 是正整数 | 0 ⇒ 客户端判"已是最新版本" |
| 8 | env 里的更新说明非空 | 更新弹窗显示「服务端未提供更新说明。」 |
| 9 | env 里的发布时间非空 | 同上，且无法判断这条说明有多旧 |
| 10 | env 的发布时间 == 版本接口的发布时间 | 服务没重启 ⇒ 接口给的是旧发布时刻 |
| 11 | env 的更新说明 == 版本接口的更新说明 | **2026-10-25 踩的坑**（0.4.2 的弹窗写着 0.4.1 的说明） |

## 用法

    from release_consistency import check_consistency
    problems = check_consistency(env=..., remote=..., api=...)
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

# 版本化 APK 的文件名形态：tingxia-<版本名>.apk
APK_NAME_RE = re.compile(r"^tingxia-(?P<version>.+)\.apk$")


def _unquote(value: str) -> str:
    """剥掉 systemd EnvironmentFile 语义下的成对引号。

    systemd 读 `KEY="v"` 时得到的是 `v`（不带引号）。我们直接读文本行，
    若不剥，引号就会被当成值的一部分 ⇒ 与版本接口（走 systemd 读到的值）
    比对时永远不相等 ⇒ 巡检永远报"不一致"。
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_block(text: str) -> dict[str, str]:
    """env 文本 → {键: 值}（只认没被注释的 `KEY=VALUE` 行）。

    为什么自己解析而不是用 `shlex`：env 文件里有 systemd 的语法
    （引号值、续行符），用 shell 解析器会得到"看起来对但语义不同"的结果。
    这里只需要 5 个简单的键值对，自己解析更可控。

    ⚠️ 值里可能有 `=`（更新说明里就可能有）⇒ 用**第一个** `=` 切分。
    """
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        out[key] = _unquote(value.strip())
    return out


def parse_remote_block(text: str) -> dict[str, str]:
    """远端探针输出 → {键: 值}（`KEY=VALUE` 每行一条）。"""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def check_consistency(
    *,
    env: dict[str, str],
    remote: dict[str, str],
    api: dict[str, Any],
) -> list[str]:
    """三处真相源对拍。返回**不一致的描述列表**（空列表 = 一致）。

    返回问题列表而不是直接 exit，是为了让测试能逐条断言
    —— "报不报错"这种笼统断言没有牙齿（抽掉一条判据、让后面那条拦住，
    用例照样绿）。这里每条判据都产出一条**可辨识**的描述。
    """
    problems: list[str] = []

    env_code = env.get("STASHBOX_APP_LATEST_VERSION_CODE", "")
    env_name = env.get("STASHBOX_APP_LATEST_VERSION_NAME", "")
    env_url = env.get("STASHBOX_APP_DOWNLOAD_URL", "")
    env_notes = env.get("STASHBOX_APP_RELEASE_NOTES", "")
    env_published = env.get("STASHBOX_APP_PUBLISHED_AT", "")

    api_code = api.get("latest_version_code")
    api_name = api.get("latest_version_name", "")
    api_url = api.get("download_url", "")

    link = remote.get("LINK", "")
    file_exists = remote.get("FILE_EXISTS") == "yes"

    # ── 判据 1/2/3：env ↔ 版本接口（服务重启后是否真的生效）──────────
    if str(api_code) != env_code:
        problems.append(
            f"env 说 versionCode={env_code!r}，版本接口说 {api_code!r}"
            " ⇒ user-service 没重启 / env 没生效"
        )
    if api_name != env_name:
        problems.append(f"env 说版本名 {env_name!r}，版本接口说 {api_name!r}")
    if api_url != env_url:
        problems.append(f"env 说下载地址 {env_url!r}，版本接口说 {api_url!r}")

    # ── 判据 4：接口下发的包 == 软链指向的包 ─────────────────────────
    if api_url:
        want_name = api_url.rsplit("/", 1)[-1]
        if want_name != link:
            problems.append(
                f"版本接口下发的包是 {want_name!r}，但 latest.apk 指向 {link!r}"
                " ⇒ 用户点更新会拿到跟营销页不一样的包"
            )
        # ── 判据 5：那个文件必须真的在磁盘上 ─────────────────────────
        if want_name and not file_exists:
            problems.append(f"latest.apk → {link!r}，但文件不在磁盘上 ⇒ 下载会 404")

    # ── 判据 6：软链名里的版本 == env 的版本名 ───────────────────────
    # 这正是 2026-11-09 踩的那个坑：包在、软链对、但 env 说旧号。
    m = APK_NAME_RE.match(link or "")
    if m and env_name and m.group("version") != env_name:
        problems.append(
            f"软链指向的版本是 {m.group('version')!r}，env 里写的是 {env_name!r}"
            " ⇒ 这正是 2026-11-09 踩的那个坑（包在、软链对、但版本接口说旧号）"
        )

    # ── 判据 7：versionCode 必须是正整数 ────────────────────────────
    # 0 = 没配 ⇒ 客户端判"latest(0) > installed"为假 ⇒ 永远"已是最新版本"
    if not env_code.isdigit() or int(env_code) <= 0:
        problems.append(
            f"env 里的 versionCode={env_code!r} 不是正整数 ⇒ 客户端会判「已是最新版本」"
        )

    # ── 判据 8/9：用户看得见的两个字段不能空 ────────────────────────
    # 更新说明为空 ⇒ 安卓更新弹窗显示「服务端未提供更新说明。」
    if not env_notes.strip():
        problems.append("env 里的更新说明（STASHBOX_APP_RELEASE_NOTES）是空的 ⇒ 用户会看到空说明")
    if not env_published.strip():
        problems.append(
            "env 里的发布时间（STASHBOX_APP_PUBLISHED_AT）是空的 ⇒ 无法判断这条说明有多旧"
        )

    # ── 判据 10：发布时间 env ↔ 接口（服务重启是否真的生效）────────────
    api_published = str(api.get("published_at") or "")
    if (
        env_published.strip()
        and api_published.strip()
        and env_published.strip() != api_published.strip()
    ):
        problems.append(
            f"env 说发布时间 {env_published!r}，版本接口说 {api_published!r} ⇒ user-service 没重启"
        )

    # ── 判据 11：更新说明 env ↔ 接口（2026-10-25 踩过的坑）──────────
    # 0.4.2 发出去后弹窗里写着 0.4.1 的功能清单 —— 错的说明不会自己暴露。
    api_notes = str(api.get("release_notes") or "")
    if env_notes.strip() and api_notes.strip() and env_notes.strip() != api_notes.strip():
        problems.append(
            f"env 里的更新说明与版本接口返回的不一致 ⇒ 用户会读到别的版本的说明"
            f"（env: {env_notes[:30]!r}… / 接口: {api_notes[:30]!r}…）"
        )

    return problems


def main() -> int:
    """命令行入口：读三个来源，对拍，打印结论。退出码 0 = 一致。"""
    try:
        api = json.loads(os.environ.get("API_JSON", "") or "{}")
    except Exception as exc:  # noqa: BLE001 —— 任何解析失败都要变成"不一致"
        print(f"✗ 版本接口返回的不是合法 JSON：{exc}")
        return 1

    env = parse_env_block(os.environ.get("ENV_BLOCK", ""))
    remote = parse_remote_block(os.environ.get("REMOTE_BLOCK", ""))
    problems = check_consistency(env=env, remote=remote, api=api)

    if problems:
        print("\n✗ 「当前版本」三处不一致：")
        for p in problems:
            print(f"   · {p}")
        print("\n   ⇒ 用 deploy/release_apk.sh 发版（别手工分步做）。")
        return 1

    print(f"  ✓ env      versionCode={env.get('STASHBOX_APP_LATEST_VERSION_CODE')}")
    print(f"  ✓ 接口     versionCode={api.get('latest_version_code')}")
    print(f"  ✓ 软链     latest.apk → {remote.get('LINK')}")
    print(f"  ✓ 下载地址 {api.get('download_url')}")
    print("\n✅ 三处一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
