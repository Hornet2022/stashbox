#!/usr/bin/env python3
"""把「当前版本」的事实一次性写进 env 文件（发版脚本调用）。

## 为什么要有这个脚本

`release_apk.sh` 原来用 `sed -i` 改 **3 个** `STASHBOX_APP_*` 变量：

    STASHBOX_APP_DOWNLOAD_URL / STASHBOX_APP_LATEST_VERSION_CODE / STASHBOX_APP_LATEST_VERSION_NAME

实测（2026-10-25 发 0.4.2）发现它**漏了两个**：

| 漏掉的键 | 谁看得到 | 漏改的后果 |
|---|---|---|
| `STASHBOX_APP_RELEASE_NOTES` | **用户**（安卓更新弹窗渲染它，见 `AppUpdateDialog.kt`） | 0.4.2 的更新弹窗里写着 0.4.1 的功能清单 ⇒ 内测用户以为「这些新功能我还没看到」⇒ **报假 bug** |
| `STASHBOX_APP_PUBLISHED_AT` | API 消费方 | 一直是 `null`，看不出这版什么时候发的 |

根因不是「忘了改」，是**没人校验**：sed 改完没有任何断言，
版本接口返回什么没人比对 ⇒ 错的说明能一直挂着，
直到有人手动点开更新弹窗才会发现。

所以这个脚本的职责不只是「写」，还有四件「写完才知道对不对」的事：

1. **写之前备份**，且备份**限量**（不留淘汰机制的备份目录会越攒越大，
   实测见过 17 个备份目录共 797MB —— 比被保护的东西还大）；
2. **拒绝重复键**（同一个键出现两次 ⇒ 谁也说不清算哪个生效）；
3. **原子写**（先写临时文件再 rename，避免写到一半的 env 把服务带崩）；
4. **写完回读校验**，值对不上就报错退出，不留半成品。

## 用法

一般由 `release_apk.sh` 调用（它把本文件喂给远端 `python3 -` 的 stdin，
这样更新说明不用穿 shell 命令行 —— 中文说明里的 `&` `|` `'` 会把引号炸掉）。

单独跑也行：

    NOTES_B64=<base64 的更新说明> python3 set_app_version_env.py \\
        --env-file /etc/stashbox/stashbox.env \\
        --version-name 0.4.3 --version-code 16 \\
        --download-url https://stashbox.cn/downloads/tingxia-0.4.3.apk \\
        --apk-path /var/www/stashbox-downloads/tingxia-0.4.3.apk

`--apk-path` 的文件 mtime 就是「这版的发布时刻」—— 不用手写一个
「看起来像发布时刻」的时间（实测那次就是取 APK 落地文件的 mtime）。

## 退出码

0 = 写成功且回读校验通过；非 0 = 拒绝写（env 原封不动）。
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# 这次发版要同步的「当前版本」事实。
#
# 为什么是 5 个而不是 3 个：前 3 个决定「能不能下载到对的包」，
# 后 2 个决定「用户看到的说明对不对」—— 后者同样是发版的一部分，
# 漏了不会报错，只会让用户读到上一版的说明。
MANAGED_KEYS = (
    "STASHBOX_APP_DOWNLOAD_URL",
    "STASHBOX_APP_LATEST_VERSION_CODE",
    "STASHBOX_APP_LATEST_VERSION_NAME",
    "STASHBOX_APP_RELEASE_NOTES",
    "STASHBOX_APP_PUBLISHED_AT",
)

# 备份保留几份。留最近 3 份足够回滚（当前 + 上一次 + 再上一次），
# 又不会像"永不淘汰的备份目录"那样越攒越大。
DEFAULT_KEEP_BACKUPS = 3


class Refuse(Exception):
    """拒绝写 env —— 抛这个错时 env 文件保持原样。"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="把当前版本的 5 个事实写进 env 文件")
    p.add_argument("--env-file", required=True, help="env 文件路径")
    p.add_argument("--version-name", required=True, help="版本名，如 0.4.3")
    p.add_argument("--version-code", required=True, help="versionCode，必须是整数")
    p.add_argument("--download-url", required=True, help="该版本的 APK 下载地址")
    p.add_argument("--apk-path", required=True, help="该版本 APK 的落地路径（取 mtime 当发布时刻）")
    p.add_argument(
        "--release-notes",
        default=None,
        help="更新说明（用户会在更新弹窗里看到）。默认读环境变量 NOTES_B64 的 base64",
    )
    p.add_argument("--backup-dir", default=None, help="备份目录，默认是 env 文件旁的 env-backups/")
    p.add_argument("--keep-backups", type=int, default=DEFAULT_KEEP_BACKUPS, help="备份保留份数")
    return p.parse_args(argv)


def resolve_notes(explicit: str | None) -> str:
    """更新说明：命令行优先，否则读 NOTES_B64（base64）。

    为什么走 base64：更新说明是用户可见的中文文本，里面可能有 `&` `|` `'`
    等字符。走 shell 命令行 + sed 替换时，`&` 会被 sed 当成「整个匹配」、
    `|` 会截断表达式、`'` 会把引号闭合掉 ⇒ 静默写坏。
    """
    if explicit is not None:
        return explicit
    b64 = os.environ.get("NOTES_B64")
    if not b64:
        raise Refuse(
            "没给更新说明：--release-notes 或环境变量 NOTES_B64 至少要有一个。"
            "更新说明是用户看得见的内容（更新弹窗会渲染它），"
            "空着发版等于让用户读到上一版的说明。"
        )
    try:
        return base64.b64decode(b64, validate=True).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - 这里要把任何解码问题都变成"拒绝写"
        raise Refuse(f"NOTES_B64 不是合法的 base64 UTF-8 文本：{exc}") from exc


def validate_notes_for_env_file(notes: str) -> None:
    """更新说明能不能安全地写进 systemd 的 EnvironmentFile。

    systemd 的 EnvironmentFile 不是"任意文本塞一行"的格式，有三条会**静默写坏**的规则：

    | 规则 | 后果 |
    |---|---|
    | 值里含换行 | 后半截变成独立的一行，systemd 当成另一个变量/垃圾行解析 ⇒ 整个文件可能起不来 |
    | 值以 `\\` 结尾 | `\\` 是 systemd 的**续行符** ⇒ 这一行会和下一行拼起来 ⇒ 后面的键全被吃掉 |
    | 值以 `"` 或 `'` 开头 | 被当成**带引号的值**解析，里面的引号/转义按另一套规则处理 ⇒ 值会被改写 |

    ⇒ 这三种直接拒绝写，而不是"写进去再说"：写坏了的服务端，
    症状是 user-service 起不来（重启后 502），排查成本比拒绝高一个量级。
    """
    if "\n" in notes or "\r" in notes:
        raise Refuse(
            "更新说明里不能有换行（systemd 的 EnvironmentFile 会把换行后的内容当成另一行解析）。"
        )
    if notes.endswith("\\"):
        raise Refuse(
            "更新说明不能以反斜杠结尾（systemd 的 EnvironmentFile 里反斜杠是续行符，会把下一行吃掉）。"
        )
    if notes[0] in "\"'":
        raise Refuse("更新说明不能以引号开头（systemd 会把它当成带引号的值解析，内容会被改写）。")


def published_at_from(apk_path: Path) -> str:
    """发布时刻 = APK 落地文件的 mtime（UTC，秒精度）。

    为什么不用 `now()`：脚本跑完还要重启服务、等接口起来，
    真正"用户能下载到这版"的时刻是文件落地那一刻，不是 env 写完那一刻。
    取一个**有出处**的时间，比手写一个时间可靠。
    """
    if not apk_path.is_file():
        raise Refuse(f"找不到 APK 文件，拿不到它的落地时间：{apk_path}")
    mtime = datetime.fromtimestamp(apk_path.stat().st_mtime, tz=timezone.utc)
    return mtime.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_env(text: str) -> tuple[list[str], dict[str, str]]:
    """拆成 (行列表, 已有的键值)。

    注意：这里**不**按 `=` 拆分后立刻建字典 —— 一个键出现两次时
    后者会覆盖前者，看起来"有值"，实际是 env 文件自己打架。
    重复键要在写之前就拒绝。
    """
    lines = text.split("\n")
    seen: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        seen[key] = stripped.split("=", 1)[1]
    return lines, seen


def find_duplicate_keys(lines: list[str], keys: tuple[str, ...]) -> list[str]:
    """返回「出现多于一次」的被管理键。"""
    counts: dict[str, int] = {k: 0 for k in keys}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in counts:
            counts[key] += 1
    return [k for k, n in counts.items() if n > 1]


def apply_values(lines: list[str], values: dict[str, str]) -> list[str]:
    """把 values 应用到行列表：已有的替换，没有的追加。

    追加的位置放在**文件末尾**：env 文件是 systemd 的 EnvironmentFile，
    同一变量名后者覆盖前者，所以追加在末尾天然是"最后生效"，
    也避免了改动文件中间的行号（diff 更干净）。
    """
    out: list[str] = []
    applied: set[str] = set()
    for line in lines:
        stripped = line.strip()
        replaced = False
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                applied.add(key)
                replaced = True
        if not replaced:
            out.append(line)
    for key, value in values.items():
        if key not in applied:
            out.append(f"{key}={value}")
    return out


def backup_env(env_file: Path, backup_dir: Path, keep: int) -> Path:
    """备份 env 并清理老备份。

    为什么备份要限量：备份目录如果没有淘汰机制，就会一直攒，
    实测见过「17 个备份目录共 797MB」—— 比被保护的东西本身还大。

    为什么文件名要带微秒 + 兜底后缀：秒级时间戳在"连着发两版"时会撞名 ——
    实测做测试时 4 次写入落在同一秒里，备份互相覆盖，份数上限形同虚设。
    ⇒ 微秒不够就再加序号，保证每次备份都是独立的一份。
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"{env_file.name}.{stamp}.bak"
    n = 1
    while target.exists():
        target = backup_dir / f"{env_file.name}.{stamp}-{n}.bak"
        n += 1
    shutil.copy2(env_file, target)

    old = sorted(backup_dir.glob(f"{env_file.name}.*.bak"))
    for stale in old[:-keep] if keep > 0 else old:
        stale.unlink()
    return target


def write_atomic(env_file: Path, lines: list[str]) -> None:
    """先写临时文件再 rename —— 避免写到一半的 env 被服务读走。

    保留原文件的权限与属主：env 文件里有密钥，权限不能因为重写而放松。
    """
    tmp = env_file.with_name(env_file.name + ".tmp-set-version")
    mode = env_file.stat().st_mode
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        if not lines or not lines[-1].endswith("\n"):
            f.write("\n")
    os.chmod(tmp, mode)
    try:
        st = env_file.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
    except (PermissionError, OSError):
        # 非 root 场景（如本地测试）改不了属主，跳过即可。
        pass
    os.replace(tmp, env_file)


def verify(env_file: Path, values: dict[str, str]) -> None:
    """回读校验：文件里读回来的值必须就是我们刚写的。"""
    text = env_file.read_text(encoding="utf-8")
    _, seen = parse_env(text)
    bad = []
    for key, want in values.items():
        got = seen.get(key)
        if got != want:
            bad.append(f"{key}: 文件里是 {got!r}，应为 {want!r}")
    if bad:
        raise Refuse("回读校验失败：\n  " + "\n  ".join(bad))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_file = Path(args.env_file)
    apk_path = Path(args.apk_path)

    if not env_file.is_file():
        print(f"✗ 找不到 env 文件：{env_file}", file=sys.stderr)
        return 1
    if not args.version_code.isdigit():
        print(f"✗ versionCode 必须是整数：{args.version_code}", file=sys.stderr)
        return 1

    try:
        notes = resolve_notes(args.release_notes)
        if not notes.strip():
            raise Refuse(
                "更新说明是空的。更新弹窗会把空说明显示成「服务端未提供更新说明。」，"
                "而且用户读到的是空白 —— 发版时应当写清楚这一版改了什么。"
            )
        validate_notes_for_env_file(notes)
        published_at = published_at_from(apk_path)

        values = {
            "STASHBOX_APP_DOWNLOAD_URL": args.download_url,
            "STASHBOX_APP_LATEST_VERSION_CODE": args.version_code,
            "STASHBOX_APP_LATEST_VERSION_NAME": args.version_name,
            "STASHBOX_APP_RELEASE_NOTES": notes,
            "STASHBOX_APP_PUBLISHED_AT": published_at,
        }

        original = env_file.read_text(encoding="utf-8")
        lines, _ = parse_env(original)

        dupes = find_duplicate_keys(lines, MANAGED_KEYS)
        if dupes:
            raise Refuse(
                "env 文件里这些键出现了多次：" + "、".join(dupes) + "。"
                "重复键意味着没人说得清算哪个生效，env 文件自己就在打架 —— "
                "请先人工清理成每个键只有一行。"
            )

        backup_dir = Path(args.backup_dir) if args.backup_dir else env_file.parent / "env-backups"
        backup_path = backup_env(env_file, backup_dir, args.keep_backups)

        new_lines = apply_values(lines, values)
        write_atomic(env_file, new_lines)
        verify(env_file, values)
    except Refuse as exc:
        print(f"✗ 拒绝写 env：{exc}", file=sys.stderr)
        return 1

    print(f"OK env={env_file}")
    print(f"OK backup={backup_path}")
    print(f"OK published_at={published_at}")
    # 更新说明可能含换行，打印成 sha256 而不是原文：
    # 调用方要比对内容时用哈希比（逐字节、不会因换行/转义而误判）。
    import hashlib

    print(f"OK release_notes_sha256={hashlib.sha256(notes.encode('utf-8')).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
