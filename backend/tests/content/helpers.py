"""content-service 单测公共 helper（CP1.7 D9 端到端）；fixture 见同目录 conftest.py。

前置：本机 PG 5432 + Redis 6379 已起，且已 `alembic upgrade head`。
"""
import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select

from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, DistilledArticle, User

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_app(name: str, rel: str):
    """服务目录名带连字符（content-service），不能直接 import，按文件加载。"""
    spec = importlib.util.spec_from_file_location(name, BACKEND_DIR / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


content_main = _load_app("_cp17_content_main", "content-service/main.py")
app = content_main.app
ai_client = sys.modules["clients.ai_client"]  # main.py 导入后即可取到（目录名带连字符）


class FakeAIClient:
    """ai_client 替身：不真发 HTTP，只记录调用参数。"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    async def trigger_distill(self, article_id: str, auth_token: str | None = None, **_kwargs):
        self.calls.append({"article_id": article_id, "auth_token": auth_token})
        if self.fail:
            return None
        return {
            "article_id": article_id,
            "task_id": f"dst_{uuid.uuid4().hex[:24]}",
            "status": "started",
        }


async def new_user(monthly_quota: int = 5) -> tuple[int, str]:
    """建一个测试用户，返回 (user_id, JWT)。"""
    async with AsyncSessionLocal() as session:
        user = User(
            open_id="cp17_" + uuid.uuid4().hex[:24],
            nickname="pytest",
            tier="free",
            monthly_quota=monthly_quota,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return int(user.id), create_access_token(str(user.id))


def client(token: str | None = None, **extra_headers) -> httpx.AsyncClient:
    """ASGI 客户端（headers 下划线转连字符，如 device_id -> device-id）。"""
    headers = {k.replace("_", "-"): v for k, v in extra_headers.items()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
    )


async def new_article(user_id: int, status: str = "pending", error: str | None = None) -> str:
    async with AsyncSessionLocal() as session:
        art = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=user_id,
            url="https://mp.weixin.qq.com/s/cp17",
            source="d9",
            status=status,
            error=error,
            favorite=False,
            skip=False,
        )
        session.add(art)
        await session.commit()
        return art.id


async def new_task(
    article_id: str,
    status: str = "queued",
    audio_url: str | None = None,
    duration_sec: int | None = None,
    tags: list | None = None,
    quality_score: float | None = None,
) -> str:
    async with AsyncSessionLocal() as session:
        task = DistilledArticle(
            id=f"dst_{uuid.uuid4().hex[:24]}",
            article_id=article_id,
            status=status,
            audio_url=audio_url,
            duration_sec=duration_sec,
            tags=tags,
            quality_score=quality_score,
        )
        session.add(task)
        await session.commit()
        return task.id


async def article_row(article_id: str) -> Article | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Article).where(Article.id == article_id))
        return result.scalar_one_or_none()


async def quota_used(user_id: int) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User.quota_used).where(User.id == user_id))
        return int(result.scalar())
