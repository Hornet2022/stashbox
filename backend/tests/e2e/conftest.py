"""真机 E2E 用例的公共装置。

设计取向（CP-E2E-HARNESS）：这层是给 agent 跑的，不是给人跑的。

1. **每个用例都必须有可断言的落点**。光截��说"点了没报错"没有意义 ——
   一律断言到后端状态（DB 行 / 接口返回）或 UI 上的具体文案。
2. **失败必须留现场**。截图 + UI dump 自动落到 /tmp/stashbox-e2e/，
   失败信息里直接带路径，不用重跑就能看。
3. **不 mock 任何东西**。整条链路真跑：真设备、真后端、真 TTS。
   代价是慢，收益是测出来的东西是真的。

环境变量：
  STASHBOX_E2E_SERIAL     设备序列号，默认 f1a9e47d
  STASHBOX_E2E_SKIP_DEVICE=1  只跑不需要设备的用例（CI 无设备时）
  STASHBOX_E2E_EVIDENCE   证据目录，默认 /tmp/stashbox-e2e
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.e2e.driver import E2EFailure, evidence_on_failure  # noqa: E402

GATEWAY = os.getenv("STASHBOX_GATEWAY", "http://127.0.0.1:8100")

# 生产网关端口。这套 e2e 用例是**真的**会建用户、建文章、派蒸馏任务的，
# 打到生产就等于往生产库灌数据并占住 TTS 队列。
#
# 2026-10-02 实测踩过：全量 pytest 跑一次，往生产库写了 5 篇文章、7 个用户、
# 5 条蒸馏记录、50 条反馈，还留下 5 个排队等 TTS 的任务（每个 4-17 分钟）。
# 根因就是这里默认 8100 —— 而 tests/conftest.py 里设的 POSTGRES_DB=stashbox_test
# 对走 HTTP 的用例**完全无效**：处理请求的是生产服务，它们连的是生产库。
#
# 与 admin-web 的 e2e/gateway.ts 同一个教训：静默的默认值比没有默认值危险。
# ⚠️ 必须用 int：`urlparse(...).port` 返回的是 int，写成字符串集合会让
#    `8100 in {"8100"}` 恒为 False —— 守卫看着在、实际从不触发，
#    比没有守卫更危险。
_PRODUCTION_PORTS = {8100, 8101, 8102, 8103, 8104}


def _assert_not_production(gateway: str) -> None:
    """守卫：这套 e2e 会真的建用户/文章/派蒸馏任务，不能打到生产。"""
    from urllib.parse import urlparse

    port = urlparse(gateway).port
    if port not in _PRODUCTION_PORTS:
        return
    if os.getenv("STASHBOX_ALLOW_PROD_E2E") == "1":
        return

    # 这里用 **skip 而不是 raise**，是 2026-10 的修正。
    #
    # 原来抛 RuntimeError，发生在 conftest 导入期 —— 属于 collection error，
    # 后果是整个 pytest 收集中断：CI 里 1000+ 个与真机 e2e 毫无关系的用例
    # 也一起跑不起来（实测退出码 2）。
    #
    # 保护意图是对的（默认值 8100 确实是生产端口，见上方注释里那次踩坑记录），
    # 但手段过钝：为了防一套用例污染生产，代价是让全套 CI 失去信号 ——
    # 这正是 content 目录被 `--ignore` 掉、进而让一批卡死的用例长期没人
    # 发现的起点。
    #
    # skip(allow_module_level=True) 保留「不跑生产 e2e」这个硬保证，同时
    # 只跳过本模块，其余测试照常收集执行。
    pytest.skip(
        f"跳过真机 e2e：STASHBOX_GATEWAY={gateway} 指向生产端口 {port}。\n"
        "  这套用例会真的建用户/文章/派蒸馏任务，打生产等于污染生产库 + 占 TTS 队列。\n"
        "  · 先起隔离后端再指过去（例如 admin-web 的 e2e-backend.sh，:18100）\n"
        "  · 或显式确认后设 STASHBOX_ALLOW_PROD_E2E=1（不建议）",
        allow_module_level=True,
    )


_assert_not_production(GATEWAY)


def pytest_configure(config):
    config.addinivalue_line("markers", "device: 需要真机在线")
    config.addinivalue_line("markers", "slow: 涉及真实蒸馏/TTS，分钟级")


def pytest_collection_modifyitems(config, items):
    """没设备就打掉所有 device 用例，而不是让它们逐个失败。"""
    if os.getenv("STASHBOX_E2E_SKIP_DEVICE") == "1":
        skip = pytest.mark.skip(reason="STASHBOX_E2E_SKIP_DEVICE=1")
        for item in items:
            if "device" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def device():
    from tests.e2e.driver import DEFAULT_SERIAL, Device

    dev = Device(DEFAULT_SERIAL)
    if not dev.is_online():
        pytest.skip(f"设备 {dev.serial} 不在线")
    if not dev.app_installed():
        pytest.skip(f"{dev.serial} 上没装 com.tingxia.audio.debug")
    return dev


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """把用例的执行结果挂到 item 上，供 app fixture 决定要不要留现场。

    没有这个钩子，`request.node.rep_call` 永远是 None，
    失败现场就静默地不会存 —— 恰恰是最需要它的时候。
    """
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture
def app(device, request):
    """冷启动 App 并保证用例之间互不干扰。

    每个用例都冷启动是有意的：Android 的状态残留（登录态、上次播放、
    滚动位置）是端到端测试最容易踩的假阳性来源。
    """
    device.launch(cold=True)
    device.clear_logcat()
    yield device
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        evidence_on_failure(device, request.node.name[:60])
    device.force_stop()


@pytest.fixture(scope="session")
def admin_token():
    """签一个 admin token，用来做跨端一致性断言（直接查后端）。"""
    from stashbox.backend.common.auth import create_access_token

    return create_access_token("9018", extra={"tier": "admin"})


@pytest.fixture(scope="session")
def owner_token():
    """签 user 1（真机在用的账号，也是多数文章的属主）的 token。

    刻意和 admin_token 分开：涉及"以某个用户身份写入"的断言
    （收听进度、配额扣减）必须用属主身份，否则写到了 admin 自己的行上，
    断言会查不到数据或者查到错的行 —— 这类"用错身份"的假失败极难定位。
    """
    from stashbox.backend.common.auth import create_access_token

    return create_access_token("1", extra={"tier": "admin"})


@pytest.fixture
def http(admin_token):
    """带 admin 鉴权的极简 HTTP 客户端。刻意不用 requests/httpx 夹具，
    保持依赖最小、报错信息直白。

    ⚠ 涉及"以某用户身份写数据"的断言请用 owner_http，不要用这个 ——
    admin_token 是 user 9018，写进去的行和真机在用的 user 1 不是同一行。
    """
    import httpx

    with httpx.Client(
        base_url=GATEWAY, timeout=30, headers={"Authorization": f"Bearer {admin_token}"}
    ) as c:
        yield c


@pytest.fixture
def owner_http(owner_token):
    """带 user 1（文章属主）鉴权的 HTTP 客户端。"""
    import httpx

    with httpx.Client(
        base_url=GATEWAY, timeout=30, headers={"Authorization": f"Bearer {owner_token}"}
    ) as c:
        yield c


@pytest.fixture
def db():
    """直连 DB 查状态。

    E2E 断言后端状态用 SQL 比调接口更直接 —— 接口可能经过缓存，
    而我们要确认的是"库里到底是什么"。
    """
    import asyncio

    import asyncpg

    dsn = os.getenv(
        "STASHBOX_TEST_DSN", "postgresql://stashbox:stashbox_dev@localhost:5432/stashbox"
    )

    async def _q(sql: str, *args):
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        yield lambda sql, *args: loop.run_until_complete(_q(sql, *args))
    finally:
        loop.close()


__all__ = ["E2EFailure", "GATEWAY"]
