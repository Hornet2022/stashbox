#!/usr/bin/env python3
"""清空 e2e 隔离栈的测试产物（文章 + 蒸馏音频 + 队列残留）。

## 为什么需要它

集成测试每建一篇文章就会**真的**派一个蒸馏任务（真 LLM + 真 TTS，单篇
100~250s）。跑几轮之后 e2e 隔离库里会攒下几十篇文章、几百 MB 音频，而 arq
队列里还压着没跑完的任务 ⇒ 后面任何一次集成测试都要先排十几分钟的队。

2026-10-25 实测：某个任务在队列里排了 **1381 秒**，测试等不到就误报超时
（症状像「链路坏了」，实际只是排在别人后面）。本脚本把「攒下的东西」清掉，
让下一次从干净起点开始。

## 与 `e2e-backend.sh reset` 的区别

`reset` 是删库重建（音色基线、用户、种子文章全没）；
`clean` 只清**测试产物**，保留基线（音色、种子用户、种子三态文章）。

## 目标从**运行时生效配置**里读，不从命令行参数读

这是本脚本最重要的一条设计。四个目标各读各的源，缺省值也各不相同：

| 目标 | 谁读 | 缺省 | 缺省意味着 |
|---|---|---|---|
| 数据库 | `settings.postgres_db` | `stashbox` | **生产库** |
| 应用缓存 redis | `settings.redis_db` | `0` | **生产缓存** |
| arq 队列 redis | 只认 `REDIS_URL` | `redis://localhost:6379/0` | **生产队列** |
| 音频落盘目录 | `settings.local_audio_dir` | `.env` 里 `/tmp/audio` | **dev 栈的目录** |

所以这里**故意不提供** `--db` / `--redis-db` / `--audio-dir` 这类"我自己
说了算"的参数 —— 那样会出现"参数说一套、进程连另一套"，而脚本自己还以为
清的是隔离库。实测过最坏情况：`POSTGRES_DB` 没设时生效库就是生产库
`stashbox`；只设 `REDIS_DB` 不设 `REDIS_URL` 时，服务读写 db 14、蒸馏任务
却入队到 db 0（e2e-backend.sh 顶部记过这个坑）。⇒ 目标一律从 settings 读，
并逐条断言"确实不是生产"。

用法（由 e2e-backend.sh 的 `clean` 子命令调用，也可单独跑）：

    POSTGRES_DB=stashbox_e2e REDIS_DB=14 REDIS_URL=redis://localhost:6379/14 \\
    STORAGE_PROVIDER=local LOCAL_AUDIO_DIR=/tmp/stashbox-e2e-backend/audio \\
    backend/.venv/bin/python backend/scripts/clean_e2e_artifacts.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]  # .../stashbox/backend
BACKEND_DIR = REPO_ROOT
# `stashbox.backend.*` 这个包名的根在**上一级**（.../work），
# 因为包目录就叫 stashbox（backend 是它的子包）。
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
# article_purge 是 content-service 的**顶层**模块（`from article_purge import ...`），
# 不是包内相对导入 ⇒ 得把 content-service 目录本身放进 sys.path。
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(BACKEND_DIR / "content-service"))

PROD_DB_NAME = "stashbox"
PROD_REDIS_DB = 0
AUDIO_EXTS = (".wav", ".m4a", ".mp3", ".aac", ".ogg")
#: 本机 dev 栈（8100-8103）的音频目录 —— 仓库 .env 里的缺省值。
#: 绝不能把它当成 e2e 的目标清掉。
DEV_AUDIO_DIRS = ("/tmp/audio",)
#: 基线种子文章的 URL 前缀（e2e-backend.sh 的 seed SQL 里那三篇
#: ready / failed / pending）。**不清它们** —— `clean` 的语义是「清测试产物、
#: 保留基线」，与 `e2e-backend.sh reset`（删库重建）的区别正在于此。
SEED_URL_PREFIX = "https://example.com/e2e/"

# 关掉 SQL 回显：settings.debug 只控制 create_engine(echo=)（common/database.py:23），
# 而回显会把「目标库 = …」这几行守卫输出淹没在 SQL 里。必须在 import settings 之前设，
# 因为 pydantic-settings 在 import 时就把值读走了。
os.environ["DEBUG"] = "false"


def _fail(msg: str) -> None:
    # 先把 stdout 冲掉：报错走 stderr，不 flush 的话输出顺序会倒过来
    # （「拒绝执行」出现在「目标库 = …」前面，看着像没解析出目标就拒了）。
    sys.stdout.flush()
    print(f"✗ 拒绝执行：{msg}", file=sys.stderr)
    raise SystemExit(2)


def _dotenv_value(key: str) -> str:
    """读 backend/.env 里的**原始**值（不经过 os.environ）。

    为什么单独读文件：common.config 会 load_dotenv 把 .env 灌进 os.environ，
    于是「我们显式导出的」和「.env 里本来就有的」在环境变量里长得一模一样，
    分不出来。dev 栈的音频目录正是这样 —— 清理时如果没显式覆盖，
    就会把 dev 栈的音频一起清掉。
    """
    env_file = BACKEND_DIR / ".env"
    if not env_file.is_file():
        return ""
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ""


def _redis_db_from_url(url: str) -> int | None:
    """从 REDIS_URL 里取 db 号；解析不出来返回 None（= arq 会用缺省 db 0）。"""
    if not url:
        return None
    path = urlparse(url).path.strip("/")
    if not path:
        return None
    try:
        return int(path)
    except ValueError:
        return None


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="清空 e2e 隔离栈的测试产物")
    ap.add_argument(
        "--queue-key",
        default="stashbox:distill",
        help="arq 队列键（见 ai-service/arq_settings.py）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只报要删什么，不真删")
    return ap.parse_args()


def guard() -> dict:
    """校验目标确实是隔离栈，返回解析出来的目标。

    每条断言都对应一个**真实踩过的坑**，所以一条条列，不合并成一个大 if ——
    报错要能直接指认是哪一条不满足。
    """
    from stashbox.backend.common.config import settings

    db_name = settings.postgres_db
    redis_db = settings.redis_db
    queue_url = os.environ.get("REDIS_URL", "")
    queue_db = _redis_db_from_url(queue_url)
    provider = settings.storage_provider.lower()
    audio_dir = Path(settings.local_audio_dir)

    print(f"  目标库     = {db_name}")
    print(f"  应用 redis = db {redis_db}")
    print(f"  队列 redis = {queue_url or '(未设 REDIS_URL)'}")
    print(f"  音频目录   = {audio_dir}")

    if db_name == PROD_DB_NAME:
        _fail(f"生效库 {db_name} 是生产库（POSTGRES_DB 没设时缺省就是它）")
    if "e2e" not in db_name:
        _fail(f"生效库 {db_name} 不含 e2e —— 本脚本只清 e2e 栈，不碰别的库")
    if redis_db == PROD_REDIS_DB:
        _fail(f"生效 redis db {redis_db} 是生产缓存（REDIS_DB 没设时缺省就是 0）")
    if queue_db is None:
        _fail("没设 REDIS_URL —— arq 只认它，缺省 redis://localhost:6379/0 就是生产队列")
    if queue_db == PROD_REDIS_DB:
        _fail(f"队列 redis db {queue_db} 是生产队列")
    if queue_db != redis_db:
        _fail(f"队列 db {queue_db} 与应用 db {redis_db} 不一致 —— 两个 redis 消费者必须同库")
    if provider != "local":
        _fail(f"STORAGE_PROVIDER={provider}，必须是 local（oss ⇒ 删的是生产 OSS 桶里的对象）")
    if not str(audio_dir).startswith("/tmp/"):
        _fail(f"音频目录 {audio_dir} 不在 /tmp 下 —— 拒绝删非临时目录")
    dev_dirs = {d for d in (_dotenv_value("LOCAL_AUDIO_DIR"), *DEV_AUDIO_DIRS) if d}
    if str(audio_dir) in dev_dirs:
        _fail(f"音频目录 {audio_dir} 是 dev 栈的目录 —— 显式设 LOCAL_AUDIO_DIR 指向 e2e 的目录")

    return {
        "db": db_name,
        "redis_db": redis_db,
        "queue_db": queue_db,
        "queue_url": queue_url,
        "audio_dir": audio_dir,
    }


async def purge_articles(dry_run: bool) -> tuple[int, list[str]]:
    """用仓里现成的 purge 逻辑删文章（按 FK 依赖清子表，手写 DELETE 必然漏）。

    返回 (清掉的文章数, **存活**的文章 id 列表)。存活集合 = 基线种子
    （URL 前缀 https://example.com/e2e/）+ **清失败的那几篇**（事务回滚了，
    它们还在，音频也不能删）—— `clean` 的语义是「清测试产物、保留基线」，
    与 `reset`（删库重建）的区别正在于此。

    单篇失败不中断整轮：失败的文章记下来继续，最后汇总 —— 清理要尽量做完，
    但失败必须可见（否则「清干净了」的印象是假的）。
    """
    from article_purge import finish_purge, purge_article
    from sqlalchemy import select

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import Article

    failed: list[tuple[str, str]] = []
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Article.id, Article.url))).all()
        keep = {r[0] for r in rows if (r[1] or "").startswith(SEED_URL_PREFIX)}
        ids = [r[0] for r in rows if r[0] not in keep]
        print(
            f"  文章：{len(ids)} 篇待清"
            f"（基线种子 {len(keep)} 篇按 URL 前缀 {SEED_URL_PREFIX} 保留）"
        )
        if dry_run:
            # dry-run 什么都没删 ⇒ 存活集合 = 全部
            return len(ids), [r[0] for r in rows]

        for aid in ids:
            try:
                keys: list[str] = []
                art = await purge_article(db, aid, audio_keys=keys)
                if art is None:
                    continue
                # commit 后再读属性：AsyncSessionLocal 配了 expire_on_commit=False，
                # 属性仍在内存里；但仍按仓里既有写法先取出来（admin_router.py:343）。
                owner = art.user_id
                await db.commit()
                await finish_purge(aid, owner, audio_keys=keys)
            except Exception as exc:  # noqa: BLE001 —— 单篇失败不该中断整轮
                await db.rollback()
                failed.append((aid, str(exc)))
        if failed:
            print(f"  ⚠ 有 {len(failed)} 篇没清掉（继续清其余的）：")
            for aid, err in failed[:5]:
                print(f"     {aid}: {err[:120]}")

    # ⚠️ 失败的那几篇**还在**（事务已回滚）⇒ 它们必须算进「存活集合」。
    # 否则它们的音频会被后面的孤儿扫描当成没人引用的文件删掉 ——
    # 变成「文章还在、音频没了」⇒ 客户端点播放直接 404。
    # 这正是「DB 里有引用、磁盘上却被删」那个最坏形状（见
    # tests/infra/test_verify_audio_consistency.py 里记的联锁）。
    surviving = sorted(keep | {aid for aid, _ in failed})
    return len(ids) - len(failed), surviving


def purge_orphan_audio(audio_dir: Path, alive_ids: set[str], dry_run: bool) -> tuple[int, int]:
    """删掉「文章已经没了、文件还在」的孤儿音频。

    扫两个位置：LocalStorage 的 key 是 `audio/{article_id}.{hash8}.{ext}`，
    但历史数据也直接落在 LOCAL_AUDIO_DIR 根下（见 content-service/main.py
    `_resolve_actual_audio_extension` 的注释「扫描两个候选位置」）。

    `voices/` 子目录**不扫**：那是音色试听（属于 tts_voices，不属于文章）。
    """
    roots = [audio_dir, audio_dir / "audio"]
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        files.extend(p for p in root.glob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    # 文件名形如 {article_id}.{hash8}.{ext} 或 {article_id}.{bitrate}k.{hash8}.{ext}
    # ⇒ 取第一段点号前的部分才是 article_id。
    orphans = [p for p in files if p.stem.split(".")[0] not in alive_ids]
    freed = sum(p.stat().st_size for p in orphans)
    print(
        f"  音频：{len(files)} 个，其中孤儿 {len(orphans)} 个" f"（{freed / 1024 / 1024:.1f} MB）"
    )
    if not dry_run:
        for p in orphans:
            p.unlink(missing_ok=True)
    return len(orphans), freed


def clear_queue(queue_url: str, queue_key: str, dry_run: bool) -> int:
    """清掉队列里排着的任务（它们属于已删的文章，跑起来只会白烧 TTS）。

    连的是 **REDIS_URL** 指向的那个库（arq 队列真正待着的地方），
    不是 settings.redis_db 拼出来的那个 —— 两者在配置正确时相同，
    但配置错了时队列才是会被真正误删/误留的那一个。
    """
    import redis

    r = redis.Redis.from_url(queue_url)
    pending = r.zcard(queue_key)
    job_keys = len(list(r.scan_iter("arq:job:*")))
    print(f"  队列 {queue_key}：待跑 {pending} 个，arq 任务键 {job_keys} 个")
    if dry_run or pending == 0:
        return pending
    r.delete(queue_key)
    for pattern in ("arq:job:*", "arq:result:*", "arq:retry:*", "arq:in-progress:*"):
        keys = list(r.scan_iter(pattern))
        if keys:
            r.delete(*keys)
    r.delete("arq:unfinished")
    return pending


async def main() -> None:
    args = parse_args()
    print("▶ 清理 e2e 产物")
    if args.dry_run:
        print("（dry-run，不真删）")
    targets = guard()

    n, alive = await purge_articles(args.dry_run)

    # purge 跑完后「存活」的只有基线种子 ⇒ 剩下的音频全是孤儿；
    # dry-run 时什么都没删 ⇒ 孤儿 = 文件名里没有对应文章的那些。
    alive_ids = set(alive)
    orphans, freed = purge_orphan_audio(targets["audio_dir"], alive_ids, args.dry_run)

    pending = clear_queue(targets["queue_url"], args.queue_key, args.dry_run)

    print(
        f"✓ 完成：文章 {n} 篇、孤儿音频 {orphans} 个"
        f"（{freed / 1024 / 1024:.1f} MB）、清队列 {pending} 个任务"
    )
    print("  下一步：跑 e2e-backend.sh up 会重新播种（音色 + ready/failed/pending 三态样本）")


if __name__ == "__main__":
    asyncio.run(main())
