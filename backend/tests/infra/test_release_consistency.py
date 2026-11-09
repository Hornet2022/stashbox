"""release_consistency.py 的判定契约（2026-11-09）。

## 这个模块防的是哪一类事故

「当前版本」这件事有**三处**记录：服务端 env、/downloads/latest.apk 软链、
磁盘上的实际文件。三处各说各的时**没有任何一处会报错** —— 页面上看起来一切正常。

2026-11-09 内测反馈「设置-版本入口无法下载新版本」，根因是手工分三步发版
（scp → 软链 → env）**漏了 env 那一步**：

    · 服务器上有 0.5.2 的包，软链也指向它  ⇒ 下载入口是好的
    · 但 env 还停在 0.5.1 ⇒ 版本接口返回 code=24
    · 安卓客户端判 `latest > installed` 为假 ⇒ 显示「已是最新版本」
    · ⇒ **根本没有下载入口** ⇒ 用户点不到下载

所以判定必须独立于发版流程（有人绕过脚本手工发版时也要能发现），
且**每条判据都能被单独测出来**。

## 怎么测：一条判据一个用例，且断言「只有这一条报」

护栏最常见的失败方式是"**写对了但没牙齿**"：抽掉一条判据、让后面那条拦住，
用例照样绿。所以这里每个用例都做两件事：

    1. 构造一份**只违反这一条判据**的数据；
    2. 断言**恰好**产出 1 条问题，且这条问题的文案能辨识出是哪条判据。

⇒ 抽掉任意一条判据，对应用例立刻变红（problems 变成 0 条，条数断言失败）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "release_consistency.py"


def _load_module():
    """加载被测模块（它不是包，用 importlib 按路径加载，同 tests/infra 的既有约定）。"""
    spec = importlib.util.spec_from_file_location("release_consistency_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load_module()

# ── 一份「三处一致」的基线数据 ────────────────────────────────────────
# 所有判据用例都以它为底，只改动**一个**字段 ⇒ 恰好触发一条判据。
BASE_ENV = {
    "STASHBOX_APP_LATEST_VERSION_CODE": "25",
    "STASHBOX_APP_LATEST_VERSION_NAME": "0.5.2",
    "STASHBOX_APP_DOWNLOAD_URL": "https://stashbox.cn/downloads/tingxia-0.5.2.apk",
    "STASHBOX_APP_RELEASE_NOTES": "0.5.2 修复：蒸馏失败时显示服务端给出的真实原因。",
    "STASHBOX_APP_PUBLISHED_AT": "2026-11-09T11:04:28Z",
}

BASE_REMOTE = {
    "LINK": "tingxia-0.5.2.apk",
    "FILE_EXISTS": "yes",
    "FILE_SIZE": "3981325",
}

BASE_API = {
    "latest_version_code": 25,
    "latest_version_name": "0.5.2",
    "download_url": "https://stashbox.cn/downloads/tingxia-0.5.2.apk",
    "release_notes": "0.5.2 修复：蒸馏失败时显示服务端给出的真实原因。",
    "published_at": "2026-11-09T11:04:28Z",
}


def _run(env_overrides=None, remote_overrides=None, api_overrides=None) -> list[str]:
    env = {**BASE_ENV, **(env_overrides or {})}
    remote = {**BASE_REMOTE, **(remote_overrides or {})}
    api = {**BASE_API, **(api_overrides or {})}
    return rc.check_consistency(env=env, remote=remote, api=api)


# ── 基线：三处一致时不该报任何问题 ───────────────────────────────────
def test_三处一致时不报任何问题():
    assert _run() == []


# ── 判据 1：env 的 versionCode == 版本接口的 versionCode ─────────────
def test_判据1_env与接口的versionCode不一致():
    problems = _run(env_overrides={"STASHBOX_APP_LATEST_VERSION_CODE": "24"})
    assert len(problems) == 1
    assert "versionCode" in problems[0] and "没重启" in problems[0]


# ── 判据 2：env 的版本名 == 版本接口的版本名 ──────────────────────────
# 注意：这里必须**同时**把软链指到 0.5.1，否则判据 6 也会报 ⇒ 不是"只违反一条"。
def test_判据2_env与接口的版本名不一致():
    problems = _run(
        env_overrides={
            "STASHBOX_APP_LATEST_VERSION_NAME": "0.5.1",
            "STASHBOX_APP_DOWNLOAD_URL": "https://stashbox.cn/downloads/tingxia-0.5.1.apk",
        },
        remote_overrides={"LINK": "tingxia-0.5.1.apk"},
        api_overrides={
            "latest_version_name": "0.5.2",
            "download_url": "https://stashbox.cn/downloads/tingxia-0.5.1.apk",
        },
    )
    assert len(problems) == 1
    assert "版本名" in problems[0]


# ── 判据 3：env 的下载地址 == 版本接口的下载地址 ──────────────────────
def test_判据3_env与接口的下载地址不一致():
    problems = _run(
        env_overrides={
            "STASHBOX_APP_DOWNLOAD_URL": "https://stashbox.cn/downloads/tingxia-0.5.1.apk"
        },
        api_overrides={"download_url": "https://stashbox.cn/downloads/tingxia-0.5.2.apk"},
    )
    assert len(problems) == 1
    assert "下载地址" in problems[0]


# ── 判据 4：接口下发的包 == 软链指向的包 ─────────────────────────────
def test_判据4_接口下发的包与软链指向不一致():
    problems = _run(
        env_overrides={
            "STASHBOX_APP_DOWNLOAD_URL": "https://stashbox.cn/downloads/tingxia-0.5.1.apk"
        },
        api_overrides={"download_url": "https://stashbox.cn/downloads/tingxia-0.5.1.apk"},
    )
    assert len(problems) == 1
    assert "latest.apk" in problems[0] and "不一样" in problems[0]


# ── 判据 5：软链指向的文件在磁盘上 ───────────────────────────────────
def test_判据5_软链指向的文件不在磁盘上():
    problems = _run(remote_overrides={"FILE_EXISTS": "no"})
    assert len(problems) == 1
    assert "404" in problems[0]


# ── 判据 6：软链名里的版本 == env 的版本名（2026-11-09 踩的那个坑）────
# 这里 env 与接口的版本名**一致**（都是 0.5.1），只有软链指着 0.5.2
# ⇒ 复刻"包在、软链对、但版本接口说旧号"那一刻的真实状态。
def test_判据6_软链版本与env版本名不一致():
    problems = _run(
        env_overrides={"STASHBOX_APP_LATEST_VERSION_NAME": "0.5.1"},
        api_overrides={"latest_version_name": "0.5.1"},
    )
    assert len(problems) == 1
    assert "软链指向的版本" in problems[0]


# ── 判据 7：versionCode 必须是正整数 ────────────────────────────────
def test_判据7_versionCode为零():
    problems = _run(
        env_overrides={"STASHBOX_APP_LATEST_VERSION_CODE": "0"},
        api_overrides={"latest_version_code": 0},
    )
    assert len(problems) == 1
    assert "不是正整数" in problems[0]


def test_判据7_versionCode不是数字():
    problems = _run(
        env_overrides={"STASHBOX_APP_LATEST_VERSION_CODE": "abc"},
        api_overrides={"latest_version_code": "abc"},
    )
    assert len(problems) == 1
    assert "不是正整数" in problems[0]


# ── 判据 8：更新说明非空 ─────────────────────────────────────────────
def test_判据8_更新说明为空():
    problems = _run(
        env_overrides={"STASHBOX_APP_RELEASE_NOTES": ""},
        api_overrides={"release_notes": ""},
    )
    assert len(problems) == 1
    assert "更新说明" in problems[0]


# ── 判据 9：发布时间非空 ─────────────────────────────────────────────
def test_判据9_发布时间为空():
    problems = _run(
        env_overrides={"STASHBOX_APP_PUBLISHED_AT": ""},
        api_overrides={"published_at": ""},
    )
    assert len(problems) == 1
    assert "发布时间" in problems[0]


# ── 判据 10：env 的发布时间 == 接口的发布时间 ────────────────────────
def test_判据10_env与接口的发布时间不一致():
    problems = _run(api_overrides={"published_at": "2026-11-01T00:00:00Z"})
    assert len(problems) == 1
    assert "发布时间" in problems[0]


# ── 判据 11：env 的更新说明 == 接口的更新说明（2026-10-25 踩过的坑）──
def test_判据11_env与接口的更新说明不一致():
    problems = _run(api_overrides={"release_notes": "0.4.1 的功能清单"})
    assert len(problems) == 1
    assert "更新说明" in problems[0]


# ── 解析器：env 文本 ─────────────────────────────────────────────────
def test_parse_env_block_跳过注释与空行():
    text = """
# 这是注释
STASHBOX_APP_LATEST_VERSION_CODE=25

not_a_key_line
"""
    assert rc.parse_env_block(text) == {"STASHBOX_APP_LATEST_VERSION_CODE": "25"}


def test_parse_env_block_值里有等号按第一个等号切分():
    # 更新说明里就可能有等号 ⇒ 不能用 rsplit 或 split("=", 1) 之外的切法
    parsed = rc.parse_env_block("STASHBOX_APP_RELEASE_NOTES=a=b=c")
    assert parsed["STASHBOX_APP_RELEASE_NOTES"] == "a=b=c"


def test_parse_env_block_剥掉成对引号():
    # systemd 读 `KEY="v"` 得到的是 `v` ⇒ 不剥引号，巡检会永远报"不一致"
    parsed = rc.parse_env_block('STASHBOX_APP_DOWNLOAD_URL="https://x/y.apk"')
    assert parsed["STASHBOX_APP_DOWNLOAD_URL"] == "https://x/y.apk"


def test_parse_env_block_不成对的引号原样保留():
    parsed = rc.parse_env_block('STASHBOX_APP_DOWNLOAD_URL="https://x/y.apk')
    assert parsed["STASHBOX_APP_DOWNLOAD_URL"] == '"https://x/y.apk'


def test_parse_env_block_空文本返回空字典():
    assert rc.parse_env_block("") == {}
    assert rc.parse_env_block(None) == {}  # type: ignore[arg-type]


# ── 解析器：远端探针输出 ─────────────────────────────────────────────
def test_parse_remote_block_逐行取值():
    parsed = rc.parse_remote_block("LINK=tingxia-0.5.2.apk\nFILE_EXISTS=yes\nFILE_SIZE=3981325\n")
    assert parsed == {
        "LINK": "tingxia-0.5.2.apk",
        "FILE_EXISTS": "yes",
        "FILE_SIZE": "3981325",
    }


def test_parse_remote_block_空文本返回空字典():
    assert rc.parse_remote_block("") == {}
    assert rc.parse_remote_block("没有等号的一行\n") == {}


# ── 命令行入口 ───────────────────────────────────────────────────────
def _run_cli(monkeypatch, env_block: str, remote_block: str, api_json: str, capsys):
    monkeypatch.setenv("ENV_BLOCK", env_block)
    monkeypatch.setenv("REMOTE_BLOCK", remote_block)
    monkeypatch.setenv("API_JSON", api_json)
    rc_code = rc.main()
    return rc_code, capsys.readouterr()


def test_cli_一致时退出0并打印三处一致(monkeypatch, capsys):
    env_text = "\n".join(f"{k}={v}" for k, v in BASE_ENV.items())
    remote_text = "\n".join(f"{k}={v}" for k, v in BASE_REMOTE.items())
    code, out = _run_cli(monkeypatch, env_text, remote_text, json.dumps(BASE_API), capsys)
    assert code == 0
    assert "三处一致" in out.out


def test_cli_不一致时退出1并点名每处(monkeypatch, capsys):
    env_text = "\n".join(f"{k}={v}" for k, v in BASE_ENV.items())
    env_text += "\nSTASHBOX_APP_LATEST_VERSION_CODE=24"
    remote_text = "\n".join(f"{k}={v}" for k, v in BASE_REMOTE.items())
    code, out = _run_cli(monkeypatch, env_text, remote_text, json.dumps(BASE_API), capsys)
    assert code == 1
    assert "三处不一致" in out.out
    assert "versionCode" in out.out


def test_cli_接口返回非法JSON时报错退出1(monkeypatch, capsys):
    code, out = _run_cli(monkeypatch, "", "", "not json", capsys)
    assert code == 1
    assert "JSON" in out.out


# ── 契约护栏：脚本必须能"真的跑起来" ────────────────────────────────
# 为什么要有这条：上面的用例全是 **import 调用**。
# 模块级代码（`sys.exit(main())`）里的错误，import 路径**根本不会执行到**
# ⇒ 实测真跑时才发现 `NameError: name 'sys' is not defined`（import 只在 main() 里做）。
# 这类"只有真跑才暴露"的缺陷，必须配一条**真跑**的用例。
def test_脚本能真跑且一致时退出0(tmp_path):
    import os
    import subprocess
    import sys

    env_text = "\n".join(f"{k}={v}" for k, v in BASE_ENV.items())
    remote_text = "\n".join(f"{k}={v}" for k, v in BASE_REMOTE.items())
    env = {
        **os.environ,
        "ENV_BLOCK": env_text,
        "REMOTE_BLOCK": remote_text,
        "API_JSON": json.dumps(BASE_API),
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "三处一致" in proc.stdout


def test_脚本能真跑且不一致时退出1(tmp_path):
    import os
    import subprocess
    import sys

    env_text = "\n".join(f"{k}={v}" for k, v in BASE_ENV.items())
    remote_text = "\n".join(f"{k}={v}" for k, v in BASE_REMOTE.items())
    api = {**BASE_API, "latest_version_code": 24}
    env = {
        **os.environ,
        "ENV_BLOCK": env_text,
        "REMOTE_BLOCK": remote_text,
        "API_JSON": json.dumps(api),
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "三处不一致" in proc.stdout


# ── 契约护栏：判据只有一份实现 ───────────────────────────────────────
def test_判据只有一份实现_巡检脚本不内嵌判定逻辑():
    """巡检脚本必须调 release_consistency.py，而不是自己再写一遍判据。

    内嵌的判据没法被测试覆盖 ⇒ 护栏没牙齿；两处实现还会各自漂移。
    """
    script = SCRIPT.parent / "verify_release_consistency.sh"
    text = script.read_text(encoding="utf-8")
    assert "release_consistency.py" in text
    # 不允许在 shell 里内嵌 python 判据（会出现 python3 - <<'PY' / python3 -c）
    assert "python3 - <<" not in text
    assert "python3 -c" not in text


def test_判据只有一份实现_发版脚本复用同一个巡检():
    """发版脚本必须复用同一份判定，而不是再写一遍。

    发版脚本自己查的是"我刚写的 vs 接口"；巡检查的是"三处 vs 接口"。
    两者口径不同，不能互相替代 ⇒ 发版收尾要跑一次巡检。
    """
    release_script = SCRIPT.parent / "release_apk.sh"
    text = release_script.read_text(encoding="utf-8")
    assert "verify_release_consistency.sh" in text
    # 必须**真的执行**它（而不是只在注释里提一句）
    assert 'bash "$(dirname "$0")/verify_release_consistency.sh"' in text
