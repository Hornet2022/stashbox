"""set_app_version_env.py 的守卫契约（2026-10-25）。

## 为什么这个脚本要有测试

发版脚本原来用 `sed -i` 改 **3 个** `STASHBOX_APP_*` 变量，漏了：

- `STASHBOX_APP_RELEASE_NOTES` —— **用户看得见**（安卓更新弹窗渲染它）；
- `STASHBOX_APP_PUBLISHED_AT` —— 发布时间。

实测（2026-10-25 发 0.4.2）：0.4.2 的更新弹窗里写着 0.4.1 的功能清单。
根因不是「忘了改」，是**没人校验** ⇒ 错的说明能一直挂着。

这个脚本就是那条校验：写 5 个键 + 写完回读比对 + 拒绝会写坏 env 的值。
它一次改动的是**生产的 env 文件**（里面还有密钥），所以它的每条边界都得有牙齿。

## 怎么测：断言「拒绝理由」，不是断言「抛了异常」

守卫是多条检查串起来的：只断言 `returncode != 0` 的话，
抽掉任意一条检查、让后面那条拦住，用例照样绿 —— 那种测试等于没写。

所以每条拒绝用例都断言 stderr 里的**特有文案**。
⇒ 抽掉任意一条检查，对应用例立刻变红。

## 怎么验证「没写坏」

拒绝类用例额外断言 **env 文件字节级没变** ——
「拒绝执行」如果其实已经把文件改了半截，那比不拒绝更糟。
"""

from __future__ import annotations

import base64
import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "set_app_version_env.py"


def _load_module():
    """加载被测脚本（它不是包，用 importlib 按路径加载，同 tests/infra 的既有约定）。"""
    spec = importlib.util.spec_from_file_location("set_app_version_env_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()

NOTES = "本版功能没有变化：加了两个性能数据采集，更新请直接覆盖安装。"


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """一个仿真的 env 文件（只放非机密内容；真实文件里有密钥，不进测试）。"""
    p = tmp_path / "stashbox.env"
    p.write_text(
        "\n".join(
            [
                "# 仿真 env：只放结构，不放真值",
                "POSTGRES_PASSWORD=not-a-real-secret",
                "STASHBOX_APP_DOWNLOAD_URL=https://stashbox.cn/downloads/tingxia-0.4.1.apk",
                "STASHBOX_APP_LATEST_VERSION_CODE=14",
                "STASHBOX_APP_LATEST_VERSION_NAME=0.4.1",
                "STASHBOX_APP_MIN_SUPPORTED_VERSION_CODE=0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return p


@pytest.fixture
def apk_file(tmp_path: Path) -> Path:
    p = tmp_path / "tingxia-0.4.3.apk"
    p.write_bytes(b"fake-apk")
    # 固定 mtime：发布时刻必须来自 APK 落地时间，而不是"脚本现在几点"
    stamp = datetime(2026, 10, 26, 2, 3, 4, tzinfo=timezone.utc).timestamp()
    os.utime(p, (stamp, stamp))
    return p


def _run(env_file: Path, apk_file: Path, *extra: str, env: dict | None = None):
    args = [
        "--env-file",
        str(env_file),
        "--version-name",
        "0.4.3",
        "--version-code",
        "16",
        "--download-url",
        "https://stashbox.cn/downloads/tingxia-0.4.3.apk",
        "--apk-path",
        str(apk_file),
        *extra,
    ]
    old = dict(os.environ)
    if env:
        os.environ.update(env)
    try:
        return mod.main(args)
    finally:
        os.environ.clear()
        os.environ.update(old)


def _kv(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


# ── 正常路径 ────────────────────────────────────────────────────────────


def test_writes_all_five_keys(env_file: Path, apk_file: Path, capsys):
    """5 个键都要写对 —— 少一个就是这次的漏（更新说明/发布时间）。"""
    assert _run(env_file, apk_file, "--release-notes", NOTES) == 0
    kv = _kv(env_file)
    assert kv["STASHBOX_APP_DOWNLOAD_URL"] == "https://stashbox.cn/downloads/tingxia-0.4.3.apk"
    assert kv["STASHBOX_APP_LATEST_VERSION_CODE"] == "16"
    assert kv["STASHBOX_APP_LATEST_VERSION_NAME"] == "0.4.3"
    assert kv["STASHBOX_APP_RELEASE_NOTES"] == NOTES
    assert kv["STASHBOX_APP_PUBLISHED_AT"] == "2026-10-26T02:03:04Z"


def test_keeps_unrelated_keys_and_permissions(env_file: Path, apk_file: Path):
    """别的键（里面还有真密钥）必须原样保留，文件权限不能因为重写而放松。"""
    env_file.chmod(0o600)
    before_mode = env_file.stat().st_mode
    assert _run(env_file, apk_file, "--release-notes", NOTES) == 0
    kv = _kv(env_file)
    assert kv["POSTGRES_PASSWORD"] == "not-a-real-secret"
    assert kv["STASHBOX_APP_MIN_SUPPORTED_VERSION_CODE"] == "0"
    assert env_file.stat().st_mode == before_mode


def test_appends_missing_keys(env_file: Path, apk_file: Path):
    """env 里本来没有的两个键要**补上**（不能假设它们一定在）。"""
    text = env_file.read_text(encoding="utf-8")
    assert "STASHBOX_APP_RELEASE_NOTES" not in text
    assert _run(env_file, apk_file, "--release-notes", NOTES) == 0
    kv = _kv(env_file)
    assert kv["STASHBOX_APP_RELEASE_NOTES"] == NOTES
    assert kv["STASHBOX_APP_PUBLISHED_AT"] == "2026-10-26T02:03:04Z"


def test_leaves_no_temp_file(env_file: Path, apk_file: Path):
    """原子写：临时文件不能留在 env 旁边（下次发版会被当成脏数据）。"""
    assert _run(env_file, apk_file, "--release-notes", NOTES) == 0
    leftovers = [p.name for p in env_file.parent.iterdir() if p.name.endswith(".tmp-set-version")]
    assert leftovers == []


def test_reads_notes_from_base64_env(env_file: Path, apk_file: Path):
    """更新说明走 base64（release_apk.sh 的传参方式）：中文和特殊字符都要原样落地。"""
    tricky = "更新说明：含 & | ' \" $ 符号，还有反斜杠 \\ 在中间"
    b64 = base64.b64encode(tricky.encode("utf-8")).decode("ascii")
    assert _run(env_file, apk_file, env={"NOTES_B64": b64}) == 0
    assert _kv(env_file)["STASHBOX_APP_RELEASE_NOTES"] == tricky


# ── 拒绝路径：每条都要断言「拒绝理由」+「文件没被动过」 ──────────────────


def test_refuses_duplicate_key(env_file: Path, apk_file: Path, capsys):
    """同一个键出现两次 ⇒ env 文件自己就在打架，谁生效说不清 ⇒ 拒绝写。"""
    with env_file.open("a", encoding="utf-8") as f:
        f.write("STASHBOX_APP_LATEST_VERSION_NAME=0.4.0\n")
    before = env_file.read_bytes()

    assert _run(env_file, apk_file, "--release-notes", NOTES) == 1
    err = capsys.readouterr().err
    assert "出现了多次" in err
    assert env_file.read_bytes() == before


def test_refuses_missing_notes(env_file: Path, apk_file: Path, capsys):
    """没给更新说明 ⇒ 拒发：用户会在更新弹窗里读到上一版的说明。"""
    before = env_file.read_bytes()
    assert _run(env_file, apk_file) == 1
    err = capsys.readouterr().err
    assert "没给更新说明" in err
    assert env_file.read_bytes() == before


def test_refuses_blank_notes(env_file: Path, apk_file: Path, capsys):
    """空说明 ≠ 没说明：空说明会让更新弹窗显示「服务端未提供更新说明。」。"""
    before = env_file.read_bytes()
    assert _run(env_file, apk_file, "--release-notes", "   ") == 1
    err = capsys.readouterr().err
    assert "更新说明是空的" in err
    assert env_file.read_bytes() == before


def test_refuses_notes_with_newline(env_file: Path, apk_file: Path, capsys):
    """含换行 ⇒ systemd 的 EnvironmentFile 会把后半截当另一行解析 ⇒ 拒写。"""
    before = env_file.read_bytes()
    assert _run(env_file, apk_file, "--release-notes", "第一行\n第二行") == 1
    err = capsys.readouterr().err
    assert "不能有换行" in err
    assert env_file.read_bytes() == before


def test_refuses_notes_ending_with_backslash(env_file: Path, apk_file: Path, capsys):
    """以反斜杠结尾 ⇒ systemd 把它当续行符，会把下一行（下一个键）吃掉 ⇒ 拒写。"""
    before = env_file.read_bytes()
    assert _run(env_file, apk_file, "--release-notes", "说明\\") == 1
    err = capsys.readouterr().err
    assert "续行符" in err
    assert env_file.read_bytes() == before


def test_refuses_notes_starting_with_quote(env_file: Path, apk_file: Path, capsys):
    """以引号开头 ⇒ systemd 按带引号的值解析，内容会被改写 ⇒ 拒写。"""
    before = env_file.read_bytes()
    assert _run(env_file, apk_file, "--release-notes", '"引号开头"') == 1
    err = capsys.readouterr().err
    assert "不能以引号开头" in err
    assert env_file.read_bytes() == before


def test_refuses_missing_apk(env_file: Path, tmp_path: Path, capsys):
    """拿不到 APK 的落地时间 ⇒ 没有可信的发布时刻 ⇒ 拒写（不手写一个时间）。"""
    before = env_file.read_bytes()
    missing = tmp_path / "not-there.apk"
    assert _run(env_file, missing, "--release-notes", NOTES) == 1
    err = capsys.readouterr().err
    assert "找不到 APK 文件" in err
    assert env_file.read_bytes() == before


def test_refuses_non_integer_version_code(env_file: Path, apk_file: Path, capsys):
    """versionCode 必须是整数：写成 '16a' 会让版本比较彻底失灵。"""
    before = env_file.read_bytes()
    assert (
        mod.main(
            [
                "--env-file",
                str(env_file),
                "--version-name",
                "0.4.3",
                "--version-code",
                "16a",
                "--download-url",
                "https://stashbox.cn/downloads/tingxia-0.4.3.apk",
                "--apk-path",
                str(apk_file),
                "--release-notes",
                NOTES,
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "versionCode 必须是整数" in err
    assert env_file.read_bytes() == before


def test_refuses_missing_env_file(tmp_path: Path, apk_file: Path, capsys):
    """env 文件不存在 ⇒ 拒写（不能凭空造一个，那会丢掉里面所有别的配置）。"""
    assert _run(tmp_path / "nope.env", apk_file, "--release-notes", NOTES) == 1
    err = capsys.readouterr().err
    assert "找不到 env 文件" in err


def test_refuses_invalid_base64(env_file: Path, apk_file: Path, capsys):
    """NOTES_B64 坏了 ⇒ 拒绝，不要"尽力解析出一个错的说明"。"""
    before = env_file.read_bytes()
    assert _run(env_file, apk_file, env={"NOTES_B64": "!!!not-base64!!!"}) == 1
    err = capsys.readouterr().err
    assert "不是合法的 base64" in err
    assert env_file.read_bytes() == before


def test_detects_write_mismatch(env_file: Path, apk_file: Path, capsys, monkeypatch):
    """写完必须**回读比对**：值对不上就报错。

    为什么这条要有专门用例：没有回读时，"写歪了"（比如版本号写成了别的）
    不会报错 —— 半个版本上线了要等到内测用户装到才发现。
    这里直接模拟"写歪"，要求它被抓住。
    """

    def bad_write(path: Path, lines: list[str]) -> None:
        path.write_text(
            "\n".join(
                line.replace(
                    "STASHBOX_APP_LATEST_VERSION_CODE=16",
                    "STASHBOX_APP_LATEST_VERSION_CODE=99",
                )
                for line in lines
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(mod, "write_atomic", bad_write)
    assert _run(env_file, apk_file, "--release-notes", NOTES) == 1
    err = capsys.readouterr().err
    assert "回读校验失败" in err
    assert "STASHBOX_APP_LATEST_VERSION_CODE" in err


# ── 备份：既要留回滚路径，又不能无限攒 ──────────────────────────────────


def test_backup_contains_original_and_is_capped(env_file: Path, apk_file: Path, tmp_path: Path):
    """备份必须能回滚（内容是**写之前**的），且份数要有上限。

    为什么要有上限：备份目录若无淘汰机制会越攒越大 ——
    实测见过 17 个备份目录共 797MB，比被保护的东西本身还大。
    """
    keep = 2
    for i in range(4):
        assert (
            mod.main(
                [
                    "--env-file",
                    str(env_file),
                    "--version-name",
                    f"0.4.{i}",
                    "--version-code",
                    str(16 + i),
                    "--download-url",
                    "https://stashbox.cn/downloads/tingxia-0.4.3.apk",
                    "--apk-path",
                    str(apk_file),
                    "--release-notes",
                    NOTES,
                    "--keep-backups",
                    str(keep),
                ]
            )
            == 0
        )

    backup_dir = tmp_path / "env-backups"
    backups = sorted(backup_dir.glob("stashbox.env.*.bak"))
    assert len(backups) == keep
    # 最新的那份备份是**上一次**写之前的内容（不是最新的）
    assert "STASHBOX_APP_LATEST_VERSION_NAME=0.4.2" in backups[-1].read_text(encoding="utf-8")
