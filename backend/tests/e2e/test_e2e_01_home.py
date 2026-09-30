"""E2E-01 启动与首页：App 能不能开、首页数据对不对。

客户视角：打开 App，第一眼看到的东西。
这一组全是"零成本"用例 —— 不触发蒸馏、不动 TTS，几秒跑完，
适合当冒烟测试：它挂了说明后面的用例都别跑了。
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.device]


def test_app_cold_start_reaches_home(device):
    """冷启动能进首页，且停在首页而不是别的页面。

    这是整条用例链的地基：起不来就什么都测不了。
    刻意测冷启动（force-stop 后再启动）——热启动会绕过进程重建，
    很多初始化 bug（DI 装配、token 恢复、DB 连接）只在冷启动暴露。
    """
    device.launch(cold=True)
    assert (
        device.PKG in device.current_activity()
    ), f"冷启动后前台不是本 App: {device.current_activity()}"
    # 首页应当出现底部导航的四个入口
    for tab in ("剪藏", "蒸馏", "订阅", "回听"):
        assert device.find(text=tab) is not None, f"首页缺少底部导航项「{tab}」"


def test_home_shows_real_articles_not_machine_codes(app):
    """首页文章副标题不出现 unknown / d9 这类机器码。

    CP-3c27e4e 的回归。source 字段是机器码（wechat_mp / d9 / browser…），
    之前直接渲染出来，用户看到的是 "d9" 这种看不懂的东西。
    修复后统一走 sourceLabelRes() 映射成中文，未知码回落「通用」。

    断言口径：页面上不得出现裸的机器码字符串。
    """
    machine_codes = {"unknown", "wechat_mp", "d9", "browser", "share", "pdf"}
    nodes = [n for n in app.dump() if n.text]
    leaked = [n.text for n in nodes if n.text.strip().lower() in machine_codes]
    assert not leaked, f"首页出现机器码副标题: {leaked}\n当前 UI: {app._ui_summary()}"


def test_home_article_entry_visible(app, db):
    """首页能看到已蒸馏完成的文章。

    端到端的"数据到位"断言：不只看 App 渲染了什么，
    还要确认后端确实有可展示的数据，否则"页面空"到底是 App 的锅
    还是后端没数据，从 UI 上分不出来。
    """
    rows = db(
        "select a.id, a.title, d.status, d.audio_url "
        "from articles a join distilled_articles d on d.article_id = a.id "
        "where a.deleted_at is null and d.status = 'done' order by d.updated_at desc limit 5"
    )
    if not rows:
        pytest.skip("库里没有已完成的蒸馏产物，跳过（本机 TTS 未跑过）")

    # 取最新一篇的标题，应能在首页找到
    title = rows[0]["title"]
    if not title:
        pytest.skip("最新产物没有标题")
    assert (
        app.find(contains=title[:12]) is not None
    ), f"库里已完成蒸馏的文章「{title[:20]}」在首页看不到\n当前 UI: {app._ui_summary()}"


def test_no_crash_in_logcat(app):
    """首页停留几秒后 logcat 里没有 FATAL / AndroidRuntime 异常。

    便宜的稳定性检查：不交互、只是看会不会自己崩。
    """
    import time

    time.sleep(3)
    log = app.logcat_recent()
    fatal = [ln for ln in log.splitlines() if "FATAL EXCEPTION" in ln or "AndroidRuntime" in ln]
    assert not fatal, "首页出现崩溃:\n" + "\n".join(fatal[:5])
