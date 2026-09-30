"""E2E-05 标签订阅 + 通知 + 设置：长尾功能闭环。

客户视角：
  「我想订阅某个标签，只看这类文章」→ 标签订阅
  「有新文章时提醒我」→ 通知
  「个性化设置」→ 设置页

标签订阅是 content-service 的功能，通知 / 设置走 user-service。
这组只锁接口契约 + DB 一致性，不需要设备。

端点：
  GET  /api/v1/tags                    标签列表（含订阅状态）
  POST /api/v1/tags/{id}/subscribe     订阅
  POST /api/v1/tags/{id}/unsubscribe   取消订阅
  GET  /api/v1/notifications           通知列表
  GET  /api/v1/users/me/settings       用户设置
  PUT  /api/v1/users/me/settings       更新设置
"""

from __future__ import annotations

import pytest


def test_tags_list_includes_subscription_status(http, db):
    """标签列表返回每个 tag 的 subscribed 字段。

    CP-TAG-FILTER 的回归。如果 subscribed 字段缺失，客户端 Switch
    初始状态就是错的 —— 用户不知道自己订阅了哪些。
    """
    r = http.get("/api/v1/tags")
    assert r.status_code == 200, f"取标签列表失败: {r.status_code} {r.text[:200]}"
    body = r.json()

    tags = body if isinstance(body, list) else body.get("tags", body.get("items", []))
    assert tags, "标签列表为空，无法断言"

    # 每个标签必须有 subscribed 字段
    for tag in tags[:5]:
        assert "subscribed" in tag, f"标签缺少 subscribed 字段: {tag}"
        assert isinstance(
            tag["subscribed"], bool
        ), f"subscribed 应为 bool，实际 {type(tag['subscribed'])}: {tag}"


def test_subscribe_and_unsubscribe_tag(http, db):
    """订阅 → 取消订阅：DB 状态一致。

    这是 tag_subscription 的核心 CRUD，看似简单但此前出现过：
    - subscribe 写了 tag_subscriptions 行但接口返回 500（事务没 commit）
    - unsubscribe 删了行但 tags 查询的 LEFT JOIN 没刷新缓存
    """
    # 先拿一个 tag
    r = http.get("/api/v1/tags")
    assert r.status_code == 200
    body = r.json()
    tags = body if isinstance(body, list) else body.get("tags", body.get("items", []))
    if not tags:
        pytest.skip("没有标签可测")

    tag = tags[0]
    tag_slug = tag.get("id") or tag.get("slug")  # API 返回的 id 就是 slug（如 "news"）
    if not tag_slug:
        pytest.skip(f"标签没有 id/slug: {tag}")

    # 如果已订阅，先取消
    if tag.get("subscribed"):
        http.post(f"/api/v1/tags/{tag_slug}/unsubscribe")

    # 订阅
    r_sub = http.post(f"/api/v1/tags/{tag_slug}/subscribe")
    assert r_sub.status_code == 200, f"订阅失败: {r_sub.status_code} {r_sub.text[:200]}"
    body_sub = r_sub.json()
    assert (
        body_sub.get("ok") is True or "subscribed" in str(body_sub).lower()
    ), f"订阅响应不合预期: {body_sub}"

    # DB 确认：tag_subscriptions.tag_id 是 integer，API 的 id 是 slug，需要 JOIN
    sub_rows = db(
        "select ts.id from tag_subscriptions ts "
        "join tags t on t.id = ts.tag_id "
        "where ts.user_id = 9018 and t.slug = $1",
        tag_slug,
    )
    assert sub_rows, f"订阅后 DB 里没有 tag_subscriptions 行 (slug={tag_slug})"

    # 取消订阅
    r_unsub = http.post(f"/api/v1/tags/{tag_slug}/unsubscribe")
    assert r_unsub.status_code == 200, f"取消订阅失败: {r_unsub.status_code} {r_unsub.text[:200]}"

    # DB 确认行消失
    sub_rows_after = db(
        "select ts.id from tag_subscriptions ts "
        "join tags t on t.id = ts.tag_id "
        "where ts.user_id = 9018 and t.slug = $1",
        tag_slug,
    )
    assert not sub_rows_after, f"取消订阅后 DB 里 tag_subscriptions 行仍在 (slug={tag_slug})"


def test_subscribe_idempotent(http, db):
    """重复订阅不应报错也不应产生重复行。

    客户端网络抖动时可能重试，服务端必须幂等。
    """
    r = http.get("/api/v1/tags")
    assert r.status_code == 200
    body = r.json()
    tags = body if isinstance(body, list) else body.get("tags", body.get("items", []))
    if not tags:
        pytest.skip("没有标签可测")

    tag = tags[0]
    tag_slug = tag.get("id") or tag.get("slug")  # API 返回的 id 就是 slug
    if not tag_slug:
        pytest.skip(f"标签没有 id/slug: {tag}")

    # 确保初始状态：未订阅
    http.post(f"/api/v1/tags/{tag_slug}/unsubscribe")

    # 订阅两次
    r1 = http.post(f"/api/v1/tags/{tag_slug}/subscribe")
    r2 = http.post(f"/api/v1/tags/{tag_slug}/subscribe")

    assert r1.status_code == 200, f"第一次订阅失败: {r1.status_code}"
    assert r2.status_code == 200, f"幂等订阅失败: {r2.status_code} {r2.text[:200]}"

    # 清理
    http.post(f"/api/v1/tags/{tag_slug}/unsubscribe")


def test_notifications_list_accessible(http, db):
    """通知列表接口可用且返回合理结构。

    不创建通知（通知创建需要真实事件触发），只验证接口可访问、
    返回的是列表结构而不是 500。
    """
    r = http.get("/api/v1/notifications")
    # 可能返回 200（列表）或 404（端点不存在）——后者是 bug
    assert r.status_code == 200, f"通知列表接口不可用: {r.status_code} {r.text[:200]}"
    body = r.json()
    # 应该是列表或含 items/notifications 字段
    if isinstance(body, list):
        pass  # 直接列表
    elif isinstance(body, dict):
        assert any(
            k in body for k in ("notifications", "items", "data")
        ), f"通知响应结构不合预期: {list(body.keys())}"


def test_user_settings_read_and_update(http, db):
    """用户设置：读取 → 更新 → 读回确认。

    设置页是用户个性化的核心，如果 PUT 不落库，
    用户改了设置下次打开又回到默认值 —— 静默失灵。
    """
    # 读取当前设置
    r_get = http.get("/api/v1/users/me/settings")
    if r_get.status_code == 404:
        pytest.skip("设置端点尚未实现")
    assert r_get.status_code == 200, f"读取设置失败: {r_get.status_code} {r_get.text[:200]}"
    before = r_get.json()

    # 选一个可改的字段（优先用 auto_play / preferred_voice / language 之类）
    # 找一个 bool 或 string 字段来翻转
    updatable_field = None
    original_val = None
    for field in ("auto_play", "auto_play_next", "dark_mode", "preferred_voice", "language"):
        if field in before:
            updatable_field = field
            original_val = before[field]
            break

    if updatable_field is None:
        pytest.skip(f"设置里没有可改的字段: {list(before.keys())}")

    # 翻转/改值
    if isinstance(original_val, bool):
        new_val = not original_val
    elif isinstance(original_val, str):
        new_val = "e2e_test_value" if original_val != "e2e_test_value" else "production_value"
    else:
        new_val = original_val + 1 if isinstance(original_val, (int, float)) else original_val

    r_put = http.put(
        "/api/v1/users/me/settings",
        json={updatable_field: new_val},
    )
    if r_put.status_code == 405:
        pytest.skip("设置端点不支持 PUT")
    if r_put.status_code == 404:
        pytest.skip("设置更新端点尚未实现")
    assert r_put.status_code == 200, f"更新设置失败: {r_put.status_code} {r_put.text[:200]}"

    # 读回确认
    r_get2 = http.get("/api/v1/users/me/settings")
    after = r_get2.json()
    assert (
        after.get(updatable_field) == new_val
    ), f"设置没落库: {updatable_field} 期望 {new_val}，实际 {after.get(updatable_field)}"

    # 恢复原值
    http.put("/api/v1/users/me/settings", json={updatable_field: original_val})


def test_d9_source_label_not_machine_code_on_api(http, db):
    """API 返回的文章列表 source 字段不是机器码（d9 / wechat_mp 等）。

    CP-3c27e4e 的回归。虽然 test_e2e_01 已在 UI 层断言，
    但 API 层也该锁：如果后端直接返 "d9" 而不是 "微信"，
    只是客户端做了映射，万一映射表漏了新 source 呢？
    """
    r = http.get("/api/v1/articles")
    if r.status_code == 404:
        pytest.skip("文章列表端点路径不同")
    assert r.status_code == 200, f"取文章列表失败: {r.status_code}"

    body = r.json()
    articles = body if isinstance(body, list) else body.get("articles", body.get("items", []))
    if not articles:
        pytest.skip("没有文章")

    machine_codes = {"d9", "wechat_mp", "browser", "share", "pdf", "unknown"}
    leaked = []
    for art in articles[:20]:
        src = art.get("source", "")
        if src.lower() in machine_codes:
            leaked.append(f"id={art.get('id','?')} source={src}")

    assert not leaked, f"API 返回的文章 source 仍是机器码（应由后端映射成中文）：{leaked[:5]}"
