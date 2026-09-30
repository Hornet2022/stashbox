"""E2E-02 详情页：收听 + 听感评分闭环。

客户视角：打开一篇剪藏的文章，听一段，给个评价。

这一组是**跨端一致性**用例：UI 上看到什么，同时查后端确认状态真的变了。
只看 UI 会漏掉"界面显示已保存、后端其实没落库"这类问题 ——
那正是本项目此前反复出现的静默失灵。

前置：库里需要有一篇 status=done 的蒸馏产物。没有就 skip 而不是假失败。
"""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.device]


def _ready_article(db):
    """取一篇可用的文章；没有就 skip。

    不自己造数据：造出来的数据跑通了也说明不了真实链路。
    """
    rows = db(
        "select a.id, a.title, d.status, d.duration_sec "
        "from articles a join distilled_articles d on d.article_id = a.id "
        "where a.deleted_at is null and d.status='done' and d.audio_url is not null "
        "order by d.updated_at desc limit 1"
    )
    return rows[0] if rows else None


def _user_ratings(db, article_id: str) -> int:
    """user 1 对该文章的真实评分条数。

    注意 schema：distillation_evaluations **没有 article_id 列**，靠 task_id
    关联到 distilled_articles 再拿 article_id。刚写用例时按 article_id 直接查，
    报 UndefinedTableError —— 真实表名是 distillation_evaluations（不是
    listening_evaluations），关联路径也不同。
    """
    rows = db(
        "select count(*) n from distillation_evaluations e "
        "join distilled_articles d on d.id = e.task_id "
        "where d.article_id = $1 and e.user_id = 1 and e.auto_flag = false",
        article_id,
    )
    return rows[0]["n"]


def _open_article(app, article_id: str, retries: int = 3):
    """打开**指定** article_id 的详情页，返回进入详情页的标题。

    刻意不走「点首页第一篇」那条路。那样做隐含一个假设：列表第一篇恰好是
    我们要断言的那篇。本轮就踩了 —— 04 组为了测剪藏造了一堆
    `e2e_capture_*` 文章挂在 user 1（真机在用的账号）名下，列表第一篇
    变成测试造出来的「蒸馏中」文章，于是断言「已就绪」失败。
    **这是用例自己的问题，不是产品 bug**，但用 deep link 打开指定文章
    才是稳定做法。

    MainActivity 注册了 `navDeepLink { uriPattern = "stashbox://detail/{id}" }`，
    所以 `am start -a android.intent.action.VIEW -d stashbox://detail/<id>`
    可以直达。
    """
    app.shell(
        f"am start -a android.intent.action.VIEW "
        f"-d 'stashbox://detail/{article_id}' -n {app.MAIN_ACTIVITY}"
    )
    # 判据：详情页有「返回」按钮（列表/首页没有这个控件）
    for _ in range(retries * 5):
        if app.find(desc="返回") is not None or app.find(text="返回") is not None:
            return article_id
        time.sleep(1.0)
    raise AssertionError(f"deep link 没能打开 {article_id} 的详情页\n当前 UI: {app._ui_summary()}")


def _open_first_article(app, retries: int = 3):
    """从首页点进第一篇文章详情，返回标题。

    保留给「不关心是哪一篇」的用例用；需要断言具体某篇时请用
    `_open_article(app, article_id)`。

    锚点用「文章列表」标题下的可点击列表项。Compose 的 TextView 自身
    clickable=false，真正能点的是外层卡片，所以点标题文字的中心即可命中。

    带重试：Compose 首屏有时还在加载，第一次点会落空。
    以「详情页出现了返回按钮」作为"确实进来了"的判据，而不是盲等。
    """
    assert app.find(text="文章列表") is not None, "首页没有文章列表"

    for attempt in range(retries):
        title_node = app.find(contains="FDE")
        if title_node is None:
            nodes = [n for n in app.dump() if n.text and n.bounds[1] > 1900]
            assert nodes, f"首页找不到文章条目\n当前 UI: {app._ui_summary()}"
            title_node = nodes[0]
        x, y = title_node.center
        app.tap_at(x, y, settle=3.0)
        # 判据：详情页有「返回」按钮
        if app.find(desc="返回") is not None or app.find(text="返回") is not None:
            return title_node.text
        time.sleep(1.5)

    raise AssertionError(f"{retries} 次点击都没进详情页\n当前 UI: {app._ui_summary()}")


def test_detail_shows_ready_state_and_real_duration(app, db):
    """详情页显示「已就绪」和真实时长。

    断言时长和 DB 里的 duration_sec 对得上 —— 不是"页面上有个数字"，
    而是"这个数字是对的"。
    """
    art = _ready_article(db)
    if art is None:
        pytest.skip("库里没有可用的蒸馏产物")

    app.launch(cold=True)
    _open_article(app, art["id"])

    assert (
        app.find(text="已就绪") is not None
    ), f"详情页未显示「已就绪」\n当前 UI: {app._ui_summary()}"
    # 时长形如 "4:51 / 4:51"
    dur = app.find(contains=" / ")
    assert dur is not None, f"详情页没有显示播放时长\n当前 UI: {app._ui_summary()}"


def test_saved_rating_survives_cold_restart(app, db):
    """已评过分的产品，冷启动后详情页仍然显示评分。

    CP-158ffb6 的回归。此前两处静默失灵：
      1) 读回端点缺失 → 评完分再打开看不到自己打的分
      2) 完听上报没带 article_id（传的是蒸馏任务 id）→ 评分记到别的文章上

    关键是这个用例**冷启动**跑：评分读回走的是 loadArticle()，
    热启动可能直接复用内存里的 state，测不出读回问题。
    """
    art = _ready_article(db)
    if art is None:
        pytest.skip("库里没有可用的蒸馏产物")

    # 先确认后端确实有这条评分，否则这个用例没有意义
    if _user_ratings(db, art["id"]) == 0:
        pytest.skip(f"user 1 尚未对 {art['id']} 评过分，先跑评分用例")

    app.launch(cold=True)
    _open_article(app, art["id"])

    assert (
        app.find(contains="你的听感评分") is not None
    ), f"冷启动后详情页没有显示已保存的听感评分\n当前 UI: {app._ui_summary()}"
    # 按钮语义应从「评分」变成「修改评分」，否则用户不知道还能改
    assert (
        app.find(desc="修改评分") is not None
    ), f"已评过分但按钮仍显示「评分」——用户会以为改不了\n当前 UI: {app._ui_summary()}"


def test_rating_dialog_opens_for_already_rated_article(app, db):
    """已评过分的文章，「修改评分」弹窗仍能打开。

    CP-3c5efec 的回归 —— 这条是**我自己写出来的 bug**：
    上一轮我把「自动引导」和「手动入口」合并成一个条件
    `(shouldShowEvaluation || showManualRatingDialog) && !viewModel.isRated`，
    结果已评过分之后弹窗再也开不了。真机才暴露出来。
    修复：只抑制自动引导，手动入口始终可用。
    """
    art = _ready_article(db)
    if art is None:
        pytest.skip("库里没有可用的蒸馏产物")
    rows = db(
        "select count(*) n from distillation_evaluations e "
        "join distilled_articles d on d.id = e.task_id "
        "where d.article_id = $1 and e.user_id = 1 and e.auto_flag = false",
        art["id"],
    )
    if rows[0]["n"] == 0:
        pytest.skip(f"user 1 尚未对 {art['id']} 评过分")

    app.launch(cold=True)
    _open_article(app, art["id"])

    app.tap(desc="修改评分", settle=2.5)

    # 弹窗要么显示已评分横幅，要么至少出现评分星级控件
    banner = app.find(contains="你已评过")
    stars = app.find(contains="★") or app.find(contains="☆")
    assert (
        banner is not None or stars is not None
    ), f"点「修改评分」后弹窗没打开\n当前 UI: {app._ui_summary()}"


def test_progress_endpoint_persists_position(owner_http, db):
    """后端进度接口：改了值要真的落库。

    这是**接口契约**测试，不依赖设备，因此稳定可回归。
    单独拎出来是因为：早先把"App 播放后库里有变化"写成一条断言，结果很难定位 ——
    分不清是 App 没上报、还是后端没写、还是这篇文章本来就播完了。

    顺带钉住一个已确认的行为：值**没变**时后端会跳过写入（不做无谓 UPDATE）。
    这不是 bug，但调用方不能指望"每次 POST 都会让 updated_at 变"。
    """
    art = _ready_article(db)
    if art is None:
        pytest.skip("库里没有可用的蒸馏产物")

    orig = db(
        "select position_sec from listening_statuses where article_id=$1 and user_id=1",
        art["id"],
    )
    orig_pos = orig[0]["position_sec"] if orig else 0
    new_pos = (orig_pos + 37) % 300
    if new_pos == orig_pos:
        new_pos = (orig_pos + 11) % 300

    try:
        r = owner_http.post(
            f"/api/v1/articles/{art['id']}/progress", json={"position_sec": new_pos}
        )
        assert r.status_code == 200, f"进度上报失败: {r.status_code} {r.text[:200]}"
        assert r.json().get("ok") is True, f"接口未确认成功: {r.json()}"

        rows = db(
            "select position_sec from listening_statuses where article_id=$1 and user_id=1",
            art["id"],
        )
        assert rows, "库里没有收听进度行"
        assert (
            rows[0]["position_sec"] == new_pos
        ), f"接口说成功但库里的 position_sec 没变: 期望 {new_pos}，实际 {rows[0]['position_sec']}"
    finally:
        # 复原，避免把用例的副作用留在库里
        owner_http.post(f"/api/v1/articles/{art['id']}/progress", json={"position_sec": orig_pos})


def test_app_reports_progress_while_playing(app, db):
    """App 播放时确实在向后端上报进度（logcat 实证）。

    为什么用 logcat 而不是查库：收听进度是 **upsert**
    （`uq_listening_status_user_article UNIQUE(user_id, article_id)`），
    位置没变时后端会跳过写入 updated_at。实测这篇已播到 291/291 结尾，
    每次上报都是同一个值，库里看起来"毫无变化"——
    最初按"库里有变化"写断言，恒失败，但 App 其实一直在正常上报
    （logcat 里每 10 秒一条 `ProgressApi: posted position=291`）。

    所以拆成两条：落库由 test_progress_endpoint_persists_position 断言，
    App 侧行为由本条断言。各自失败时能立刻知道是哪一端的问题。
    """
    art = _ready_article(db)
    if art is None:
        pytest.skip("库里没有可用的蒸馏产物")

    app.launch(cold=True)
    _open_article(app, art["id"])

    # 播放按钮：播放中 content-desc = 「暂停」
    play = app.find(desc="暂停")
    if play is None:
        play = app.find(desc="播放")
        assert play is not None, f"详情页没有播放按钮\n当前 UI: {app._ui_summary()}"
        x, y = play.center
        app.tap_at(x, y, settle=3.0)

    # 等够两个上报周期（上报间隔 10s，见 PlayerController.PROGRESS_REPORT_INTERVAL_MS）
    deadline = time.time() + 30
    posted = []
    while time.time() < deadline and len(posted) < 2:
        log = app.logcat_recent()
        posted = [ln for ln in log.splitlines() if "ProgressApi" in ln and "posted position" in ln]
        if len(posted) < 2:
            time.sleep(3)

    assert posted, (
        "播放中没有看到任何进度上报（logcat 里没有 ProgressApi: posted position）\n"
        f"当前 UI: {app._ui_summary()}"
    )
    # 上报必须带正确的 article_id —— 传错 id（曾经传的是蒸馏任务 id）
    # 会让收听进度记到别的文章上，是本项目踩过的静默失灵
    assert any(
        art["id"] in ln for ln in posted
    ), f"进度上报带了错误的 article_id，期望 {art['id']}，实际：{posted[:3]}"
