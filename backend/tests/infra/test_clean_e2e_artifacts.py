"""clean_e2e_artifacts.py 的守卫契约（2026-10-25）。

## 为什么守卫必须有测试

`backend/scripts/clean_e2e_artifacts.py` 会**删文章、删音频文件、清队列**。
它的全部安全边界都在 `guard()` 里 —— 目标库不是 e2e、redis db 是 0、
存储是 oss、音频目录指向 dev 栈，任何一条不满足都必须**拒绝执行**。

这些不是「锦上添花的检查」，而是**唯一**的防线：
`get_storage()` 的缺省是 **oss**（生产桶），`Settings.postgres_db` 的缺省是
**`stashbox`（生产库）**，`Settings.redis_db` 的缺省是 **0**，
arq 队列的缺省是 `redis://localhost:6379/0`（生产队列）。
⇒ 只要守卫漏掉一条，脚本就会**静默地**作用在生产上。

## 怎么测：断言「拒绝理由」，不是断言「抛了异常」

第一版这里只断言 `pytest.raises(SystemExit)` —— **那样的测试是假的**：
守卫里任何一条先触发，异常照样抛，测试照样绿。实测做过变异检验：
把「生产库」那条检查删掉，12 条用例**全绿**（因为后面「库名不含 e2e」
那条又拦住了），删掉「队列 db 0」那条也一样（被「队列与应用库不一致」拦下）。

⇒ 现在每条用例都断言**是哪一条理由**拒绝的（stderr 里那句特有文案）。
这样删掉任意一条检查，对应用例立刻变红。

## 怎么读生效配置

守卫读的是 `stashbox.backend.common.config.settings` 上的**生效值**。
注意它读的不是 `get_settings()`（那个被 lru_cache 住、`cache_clear()` 也不影响
模块级 `settings` 变量），所以这里直接 monkeypatch 生效值本身，
测的是「守卫对某个生效组合的判断」，而不是 env → settings 的映射
（那是应用侧的事，另有人管）。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND_DIR / "scripts" / "clean_e2e_artifacts.py"

# 一套「全都对」的生效值（e2e 隔离栈的真实配置）。
OK_DB = "stashbox_e2e"
OK_REDIS_DB = 14
OK_QUEUE_URL = "redis://localhost:6379/14"
OK_AUDIO_DIR = "/tmp/stashbox-e2e-backend/audio"


@pytest.fixture
def clean_mod():
    """加载被测脚本（它不是包，用 importlib 按路径加载）。"""
    old_debug = os.environ.get("DEBUG")
    spec = importlib.util.spec_from_file_location("clean_e2e_artifacts_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    # 脚本在 import 时把 DEBUG 设成 false（关 SQL 回显），测完还原
    if old_debug is None:
        os.environ.pop("DEBUG", None)
    else:
        os.environ["DEBUG"] = old_debug


@pytest.fixture
def settings_patch(monkeypatch):
    """把生效配置改成「全都对」的那一套，单个用例再按需改一项。"""
    from stashbox.backend.common.config import settings

    monkeypatch.setattr(settings, "postgres_db", OK_DB)
    monkeypatch.setattr(settings, "redis_db", OK_REDIS_DB)
    monkeypatch.setattr(settings, "storage_provider", "local")
    monkeypatch.setattr(settings, "local_audio_dir", OK_AUDIO_DIR)
    monkeypatch.setenv("REDIS_URL", OK_QUEUE_URL)
    return settings


def _expect_refusal(clean_mod, capsys, expect: str) -> None:
    """守卫必须拒绝，且**必须是因为这一条**理由。

    只断言「抛了 SystemExit」是不够的（见模块 docstring 的变异检验）。
    """
    with pytest.raises(SystemExit):
        clean_mod.guard()
    err = capsys.readouterr().err
    assert expect in err, f"拒绝理由不是「{expect}」，实际是：{err.strip()}"


def test_脚本存在():
    assert SCRIPT.is_file(), f"找不到 {SCRIPT}"


def test_全部正确时_守卫放行(clean_mod, settings_patch):
    """基准：配置对了就必须放行 —— 否则守卫会变成「永远拒绝」，也没用。"""
    targets = clean_mod.guard()
    assert targets["db"] == OK_DB
    assert targets["redis_db"] == OK_REDIS_DB
    assert targets["queue_db"] == OK_REDIS_DB


# ---------------------------------------------------------------------------
# 逐条：这些组合必须被拒绝（且理由要对得上）
# ---------------------------------------------------------------------------


def test_生产库_被拒(clean_mod, settings_patch, capsys, monkeypatch):
    """POSTGRES_DB 没设时的缺省就是生产库 —— 这是最危险的组合。"""
    monkeypatch.setattr(settings_patch, "postgres_db", "stashbox")
    _expect_refusal(clean_mod, capsys, "是生产库")


def test_非e2e库_被拒(clean_mod, settings_patch, capsys, monkeypatch):
    """只清 e2e 栈 —— 别的库（哪怕不是生产）也不该被这个脚本动。"""
    monkeypatch.setattr(settings_patch, "postgres_db", "stashbox_test")
    _expect_refusal(clean_mod, capsys, "不含 e2e")


def test_生产缓存db0_被拒(clean_mod, settings_patch, capsys, monkeypatch):
    monkeypatch.setattr(settings_patch, "redis_db", 0)
    _expect_refusal(clean_mod, capsys, "是生产缓存")


def test_没设REDIS_URL_被拒(clean_mod, settings_patch, capsys, monkeypatch):
    """arq 只认 REDIS_URL，缺省是 db 0（生产队列）。

    只设 REDIS_DB 不设 REDIS_URL 时：服务读写 db 14、蒸馏任务却入队到 db 0 ——
    这个坑 e2e-backend.sh 顶部记过一次，守卫必须挡住同一形状。
    """
    monkeypatch.delenv("REDIS_URL", raising=False)
    _expect_refusal(clean_mod, capsys, "没设 REDIS_URL")


def test_队列指向db0_被拒(clean_mod, settings_patch, capsys, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    _expect_refusal(clean_mod, capsys, "是生产队列")


def test_队列与应用库不一致_被拒(clean_mod, settings_patch, capsys, monkeypatch):
    """两个 redis 消费者（应用缓存 / arq 队列）必须在同一个库。

    不一致时队列会残留在另一个库里 —— 清理「看起来成功」，实际队列还在。
    """
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/13")
    _expect_refusal(clean_mod, capsys, "不一致")


def test_oss存储_被拒(clean_mod, settings_patch, capsys, monkeypatch):
    """oss ⇒ finish_purge 删的是**生产 OSS 桶**里的对象。"""
    monkeypatch.setattr(settings_patch, "storage_provider", "oss")
    _expect_refusal(clean_mod, capsys, "必须是 local")


def test_音频目录不在tmp_被拒(clean_mod, settings_patch, capsys, monkeypatch):
    monkeypatch.setattr(settings_patch, "local_audio_dir", "/Users/hornet/work/audio")
    _expect_refusal(clean_mod, capsys, "不在 /tmp 下")


def test_音频目录是dev栈目录_被拒(clean_mod, settings_patch, capsys, monkeypatch):
    """/tmp/audio 是 **dev 栈**（8100-8103）的目录，不是 e2e 的。

    清 e2e 时如果没显式覆盖 LOCAL_AUDIO_DIR，就会把 dev 栈的音频一起清掉。
    """
    monkeypatch.setattr(settings_patch, "local_audio_dir", "/tmp/audio")
    _expect_refusal(clean_mod, capsys, "dev 栈的目录")


# ---------------------------------------------------------------------------
# 口径：脚本不能提供「我自己说了算」的目标参数
# ---------------------------------------------------------------------------


def test_parse_args_只接受_dry_run_和_queue_key(clean_mod, monkeypatch):
    """命令行**不能**带 --db / --redis-db / --audio-dir。

    理由：那些参数会造成「参数说一套、进程连另一套」——
    守卫按生效值判断、删除按参数执行，两者不一致时脚本会清到别的地方。
    目标一律从运行时生效配置里读。

    这条把参数面钉住：将来有人「顺手加个 --db 方便指定库」，
    最先炸的是这里，而不是生产库。
    """
    monkeypatch.setattr("sys.argv", ["clean_e2e_artifacts.py", "--dry-run"])
    ns = clean_mod.parse_args()
    assert ns.dry_run is True
    assert ns.queue_key == "stashbox:distill"
    assert not hasattr(ns, "db")
    assert not hasattr(ns, "redis_db")
    assert not hasattr(ns, "audio_dir")
