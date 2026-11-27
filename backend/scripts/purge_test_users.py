#!/usr/bin/env python3
"""运营侧批量清理任务：物理删除测试/已注销账号及其全部关联数据。

为什么需要这个脚本
------------------
`user-service` 的注销端点（POST /api/v1/users/me/account/delete）做的是**软删**：
匿名化 + 文章软删 + 停用令牌 + 删同意记录 + 吊销 token + 软删用户行。
它的 docstring 明确写着：

    「为什么是软删而不是硬删 … 真要物理删除，是运营侧的批量清理任务（30 天期限），
      不在用户请求里做」

⇒ 这个脚本就是那个**运营侧批量清理任务**。契约与注销端点同源，不另立一套。

判据（全部显式参数化，不写死"谁是谁"）
------------------------------------
* ``--keep-ids``：**永不触碰**的用户 id（系统占位 / 管理员 / 真人内测用户）。
* ``--scope all-but-keep``：目标 = users 表里除 keep 之外的全部用户。
* ``--target-ids``：显式列出目标用户（与 scope 二选一）。

失败即停（fail-closed）
-----------------------
* 目标集合为空 ⇒ 拒绝执行（防"筛选条件写错 ⇒ 静默什么都没删"）。
* 目标集合与 keep 集合有交集 ⇒ 拒绝执行。
* ``--execute`` 必须同时带 ``--i-have-a-backup``，否则拒绝（清理前必须已备份）。

删除顺序（按 FK 实测依赖，**不是**按表名猜）
--------------------------------------------
users 的子表只有 push_notifications / tag_subscriptions / user_tts_preferences 是
ON DELETE CASCADE；articles / device_tokens / favorites / feedback_v2 /
later_listens / listening_statuses / user_consents / user_listening_patterns /
few_shot_examples / distillation_evaluations 都是 NO ACTION ⇒ 必须应用层按序清。

    1. 文章：purge_article（复用 content-service 的既有实现，**不重写**）
       → favorites/later_listens/listening_statuses、蒸馏产物、音频变体、
         评分引用置 NULL；audio_keys 必须在删行前收集（带 hash 的新格式 key）
    2. commit → finish_purge：失效 Redis 缓存 + 删磁盘/OSS 音频文件（best-effort）
    3. 用户级关联：device_tokens / user_consents / listening_statuses /
       user_listening_patterns / few_shot_examples / feedback_v2 /
       distillation_evaluations / later_listens / favorites / feedback(埋点)
    4. 删 users 行
    5. **吊销该用户已签发的 token**（revocation.revoke）
       —— 这一步**不能漏**：require_user 只验 JWT 签名 + 吊销态，**不查库**
       ⇒ 不吊销的话，删掉用户行之后那些测试账号的旧 token 仍然能调接口建新数据。

用法
----
    cd backend
    .venv/bin/python scripts/purge_test_users.py --scope all-but-keep --keep-ids 0,1,23
    .venv/bin/python scripts/purge_test_users.py --scope all-but-keep --keep-ids 0,1,23 \
        --execute --i-have-a-backup
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
# `stashbox.backend.*` 这个包名的根在**上一级**（.../work），因为包目录就叫 stashbox。
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
# article_purge 是 content-service 的**顶层**模块（`from article_purge import ...`），
# 不是包内相对导入 ⇒ 得把 content-service 目录本身放进 sys.path。
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(BACKEND_DIR / "content-service"))

from article_purge import finish_purge, purge_article  # noqa: E402
from sqlalchemy import text  # noqa: E402

from stashbox.backend.common import cache_service, revocation  # noqa: E402
from stashbox.backend.common.database import AsyncSessionLocal  # noqa: E402
from stashbox.backend.common.logging import get_logger  # noqa: E402

log = get_logger("scripts.purge_test_users")

# 用户级关联表：(表名, 说明) —— 全部按 user_id 清理
USER_SCOPED_TABLES = [
    ("device_tokens", "推送令牌（含设备标识）"),
    ("user_consents", "隐私同意记录（关于这个人的个人信息）"),
    ("listening_statuses", "收听状态"),
    ("user_listening_patterns", "收听画像"),
    ("few_shot_examples", "少样本示例"),
    ("feedback_v2", "v2 反馈"),
    ("later_listens", "稍后听"),
    ("favorites", "收藏"),
    ("user_tts_preferences", "音色偏好"),
    ("tag_subscriptions", "标签订阅"),
    ("push_notifications", "推送记录"),
]


async def _raw_delete(db, sql: str, params: dict) -> int:
    """按原始 SQL 删行。

    为什么不用 `delete(text("表名"))`：SQLAlchemy 2.0 的 delete() 要求真正的 Table
    对象，text() 传进去会抛
    「subject table for an INSERT, UPDATE or DELETE expected, got TextClause」。
    ⇒ 用户级关联表一律走原始 SQL。表名来自**硬编码白名单**（不是外部输入），
    不存在注入面。
    """
    res = await db.execute(text(sql), params)
    return int(res.rowcount or 0)


async def pick_targets(db, keep_ids: list[int], target_ids: list[int] | None):
    """选目标用户 + 统计。返回 (users, articles_by_user)。"""
    if target_ids:
        ids = sorted(set(target_ids))
    else:
        rows = (await db.execute(text("select id from users"))).all()
        ids = sorted(int(r[0]) for r in rows if int(r[0]) not in keep_ids)

    overlap = set(ids) & set(keep_ids)
    if overlap:
        raise SystemExit(f"❌ 目标集合与保留集合重叠：{sorted(overlap)}（拒绝执行）")
    if not ids:
        raise SystemExit("❌ 目标集合为空（拒绝执行：筛选条件可能写错了）")

    users = []
    for uid in ids:
        row = (
            (
                await db.execute(
                    text(
                        "select id, nickname, open_id, tier, quota_used, quota_version, "
                        "created_at, deleted_at from users where id = :i"
                    ),
                    {"i": uid},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            continue
        n_art = (
            await db.execute(text("select count(*) from articles where user_id = :i"), {"i": uid})
        ).scalar()
        n_fb = (
            await db.execute(text("select count(*) from feedback where user_id = :i"), {"i": uid})
        ).scalar()
        users.append((dict(row), int(n_art), int(n_fb)))

    articles: dict[int, list[str]] = {}
    if users:
        uids = [u[0]["id"] for u in users]
        rows = (
            await db.execute(
                text("select id, user_id from articles where user_id = any(:ids)"),
                {"ids": uids},
            )
        ).all()
        for aid, uid in rows:
            articles.setdefault(int(uid), []).append(aid)

    return users, articles


def print_manifest(users, articles, db_name: str) -> None:
    print(f"目标库 = {db_name}")
    print(f"目标用户 {len(users)} 个：")
    for u, n_art, n_fb in users:
        st = "已注销" if u["deleted_at"] else "未注销"
        print(
            f"  id={u['id']:<3} nickname={str(u['nickname'])[:10]:<10} tier={u['tier']:<6} "
            f"open_id={str(u['open_id'])[:28]:<28} {st}  文章={n_art:<3} 埋点={n_fb}"
        )
    print(f"合计待删文章 {sum(len(v) for v in articles.values())} 篇")


async def purge_user(
    db, user_id: int, article_ids: list[str], delete_feedback: bool, quota_version: int = 0
) -> dict:
    """删一个用户的全部数据。返回统计。"""
    stats: dict = {"articles": 0, "tables": {}, "users_row": 0, "failed_tables": []}

    # ── 1. 文章（复用 content-service 的 purge_article，不重写）──────────
    audio_keys_by_article: list[tuple[str, list[str]]] = []
    for aid in article_ids:
        audio_keys: list[str] = []
        await purge_article(db, aid, audio_keys=audio_keys)
        stats["articles"] += 1
        audio_keys_by_article.append((aid, audio_keys))

    await db.commit()

    # ── 2. 音频文件 + 缓存（**commit 之后**才删文件）────────────────────
    for aid, keys in audio_keys_by_article:
        await finish_purge(aid, user_id, audio_keys=keys)

    # ── 3. 用户级关联表 ────────────────────────────────────────────────
    for table, _desc in USER_SCOPED_TABLES:
        try:
            n = await _raw_delete(db, f"delete from {table} where user_id = :uid", {"uid": user_id})
        except Exception as exc:
            await db.rollback()
            log.warning("table_clean_failed", table=table, error=str(exc))
            stats["failed_tables"].append(f"{table}: {exc}")
            n = 0
        if n:
            stats["tables"][table] = n

    # 蒸馏评分：两个引用列都要清（evaluator_id 与 user_id）
    try:
        n = await _raw_delete(
            db,
            "delete from distillation_evaluations where user_id = :uid or evaluator_id = :uid",
            {"uid": user_id},
        )
        if n:
            stats["tables"]["distillation_evaluations"] = n
    except Exception as exc:
        await db.rollback()
        log.warning("table_clean_failed", table="distillation_evaluations", error=str(exc))
        stats["failed_tables"].append(f"distillation_evaluations: {exc}")

    if delete_feedback:
        try:
            n = await _raw_delete(db, "delete from feedback where user_id = :uid", {"uid": user_id})
            if n:
                stats["tables"]["feedback"] = n
        except Exception as exc:
            await db.rollback()
            log.warning("table_clean_failed", table="feedback", error=str(exc))
            stats["failed_tables"].append(f"feedback: {exc}")

    # ── 4. users 行 ────────────────────────────────────────────────────
    try:
        stats["users_row"] = await _raw_delete(
            db, "delete from users where id = :uid", {"uid": user_id}
        )
    except Exception as exc:
        await db.rollback()
        log.warning("user_row_delete_failed", user_id=user_id, error=str(exc))
        stats["failed_tables"].append(f"users: {exc}")
        stats["users_row"] = 0
    await db.commit()

    # ── 5. 吊销 token（**不能漏**：require_user 不查库）──────────────────
    await revocation.revoke(user_id)

    # ── 6. 清用户态缓存（quota / pending，TTL 60s，但顺手清干净）────────
    try:
        await cache_service.invalidate_quota(user_id, int(quota_version) + 1)
    except Exception as exc:
        log.warning("quota_cache_invalidate_failed", user_id=user_id, error=str(exc))
    try:
        await cache_service.invalidate_pending(user_id)
    except Exception as exc:
        log.warning("pending_cache_invalidate_failed", user_id=user_id, error=str(exc))

    return stats


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-ids", default="0,1", help="永不触碰的用户 id（逗号分隔）")
    ap.add_argument("--scope", choices=["all-but-keep"], default=None)
    ap.add_argument("--target-ids", default=None, help="显式指定目标用户 id（逗号分隔）")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--i-have-a-backup", action="store_true")
    ap.add_argument("--keep-feedback", action="store_true", help="保留 feedback 埋点（默认删）")
    ap.add_argument("--json-out", default=None, help="把清理报告写到该 JSON 文件")
    args = ap.parse_args()

    keep_ids = [int(x) for x in args.keep_ids.split(",") if x.strip()]
    target_ids = (
        [int(x) for x in args.target_ids.split(",") if x.strip()] if args.target_ids else None
    )
    if target_ids is None and args.scope != "all-but-keep":
        ap.error("必须给 --scope all-but-keep 或 --target-ids")

    from stashbox.backend.common.config import settings

    async with AsyncSessionLocal() as db:
        users, articles = await pick_targets(db, keep_ids, target_ids)

    print_manifest(users, articles, settings.postgres_db)
    if not args.execute:
        print("\n（dry-run：未执行任何删除。确认清单后加 --execute --i-have-a-backup）")
        return 0

    if not args.i_have_a_backup:
        raise SystemExit("❌ 拒绝执行：必须先备份（--i-have-a-backup 是你已备份的确认）")

    target_ids_list = [u[0]["id"] for u in users]
    report: dict = {"db": settings.postgres_db, "kept": keep_ids, "users": {}}
    async with AsyncSessionLocal() as db:
        for u, _n_art, _n_fb in users:
            uid = u["id"]
            stats = await purge_user(
                db,
                uid,
                articles.get(uid, []),
                not args.keep_feedback,
                quota_version=int(u.get("quota_version") or 0),
            )
            report["users"][str(uid)] = {
                "nickname": u["nickname"],
                "open_id": u["open_id"],
                "deleted_at": str(u["deleted_at"]) if u["deleted_at"] else None,
                **stats,
            }
            print(
                f"  ✔ uid={uid} 文章={stats['articles']} users行={stats['users_row']} "
                f"关联表={stats['tables']}"
            )

        # ── 校验：目标集合必须归零，保留集合必须完好 ────────────────────
        print("\n=== 清理后校验 ===")
        left = (
            await db.execute(
                text("select count(*) from users where id = any(:ids)"),
                {"ids": target_ids_list},
            )
        ).scalar()
        left_art = (
            await db.execute(
                text("select count(*) from articles where user_id = any(:ids)"),
                {"ids": target_ids_list},
            )
        ).scalar()
        left_fb = (
            await db.execute(
                text("select count(*) from feedback where user_id = any(:ids)"),
                {"ids": target_ids_list},
            )
        ).scalar()
        kept_users = (
            await db.execute(
                text("select count(*) from users where id = any(:ids)"), {"ids": keep_ids}
            )
        ).scalar()
        kept_art = (
            await db.execute(
                text("select count(*) from articles where user_id = any(:ids)"),
                {"ids": keep_ids},
            )
        ).scalar()
        kept_fb = (
            await db.execute(
                text("select count(*) from feedback where user_id = any(:ids)"),
                {"ids": keep_ids},
            )
        ).scalar()
        print(f"  目标 users 残留 = {left}（应为 0）")
        print(f"  目标 articles 残留 = {left_art}（应为 0）")
        print(f"  目标 feedback 残留 = {left_fb}（应为 0）")
        print(f"  保留 users = {kept_users}（应为 {len(keep_ids)}）")
        print(f"  保留用户的文章数 = {kept_art}（应与清理前一致）")
        print(f"  保留用户的埋点数 = {kept_fb}（应与清理前一致）")

        # ── 逐表残留检查（真正的 fail-closed：不能只看三张表）────────────
        print("\n=== 逐表残留检查（目标集合）===")
        residual: dict[str, int] = {}
        checks = [
            ("users", "select count(*) from users where id = any(:ids)"),
            ("articles", "select count(*) from articles where user_id = any(:ids)"),
            (
                "distilled_articles",
                "select count(*) from distilled_articles d join articles a on a.id=d.article_id"
                " where a.user_id = any(:ids)",
            ),
            (
                "audio_variants",
                "select count(*) from article_audio_variants v join distilled_articles d"
                " on d.id=v.distilled_article_id join articles a on a.id=d.article_id"
                " where a.user_id = any(:ids)",
            ),
            ("feedback", "select count(*) from feedback where user_id = any(:ids)"),
        ] + [
            (t, f"select count(*) from {t} where user_id = any(:ids)")
            for t, _ in USER_SCOPED_TABLES
        ]
        for name, sql in checks:
            try:
                n = (await db.execute(text(sql), {"ids": target_ids_list})).scalar()
            except Exception as exc:
                n = -1
                print(f"  {name}: 查询失败 {exc}")
            residual[name] = int(n or 0)
            flag = "OK" if int(n or 0) == 0 else "❌ 残留"
            print(f"  {name:<26} {n:>6}  {flag}")

        ok = (
            left == 0
            and left_art == 0
            and left_fb == 0
            and kept_users == len(keep_ids)
            and all(v == 0 for v in residual.values())
        )
        if not ok:
            for uid, st in report["users"].items():
                if st.get("failed_tables"):
                    print(f"  ⚠️ uid={uid} 失败的表: {st['failed_tables']}")
        report["verify"] = {
            "target_users_left": left,
            "target_articles_left": left_art,
            "target_feedback_left": left_fb,
            "kept_users": kept_users,
            "kept_articles": kept_art,
            "kept_feedback": kept_fb,
            "residual_by_table": residual,
            "ok": ok,
        }
        print(f"\n判定：{'✅ 清理完成且校验通过' if ok else '❌ 校验未通过（见上）'}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"报告写入 {args.json_out}")
    return 0 if report["verify"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
