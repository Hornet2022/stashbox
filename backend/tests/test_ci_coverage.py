"""CI 到底跑了哪些测试 —— 把这件事钉成断言（2026-10）。

## 为什么要有这个文件

两批测试曾经长期躺在 CI 之外，机制完全不同，但**后果一样**：没人知道它们已经
不跑了，于是它们各自烂掉：

  1. `tests/` 下 5 条硬编码 `--ignore`（2026-10 已收敛）
  2. `content-service/tests/` **整目录**不在 CI 的 pytest 命令里 —— 150 个用例

第 2 批被发现时，里面已经积了 4 failed + 1 error + 13 skip，全是陈旧测试：
回调端点加了 fail-closed 密钥守卫后测试没跟上、正文长度阈值提到 200 字后
夹具没跟上、测试账号靠「dev 库里必然存在」这个假设。

这些烂点每一条**单独**看都不难修，难的是「怎么保证它不再默默躺在 CI 之外」。
靠注释是不够的 —— 上一轮丢信号的原因恰恰就是「该跑什么」只写在注释里，
而注释不会因为命令改了而报警。

所以这里把 workflow 本身当被测对象：断言它确实覆盖了两棵树、确实没有整目录
--ignore、确实**不**在命令里写 `-m`（marker 口径统一在 pyproject 的 addopts）。

## 还有一个隐蔽的失败模式

报告 artifact 如果只写一个文件，两棵树的报告会互相覆盖 ——
「只剩 content-service 的报告」在 Actions 界面上看起来完全正常，
和「两棵树都跑了」没有区别。这里一并断言。
"""

import re
from pathlib import Path

import yaml

# 本文件在 <repo>/backend/tests/ 下，所以：
#   parents[0] = backend/tests, [1] = backend, [2] = <repo>
BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
PYPROJECT = BACKEND_DIR / "pyproject.toml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _pytest_run_blocks() -> list[str]:
    """所有 pytest 步骤的 run 脚本。"""
    steps = _workflow()["jobs"]["pytest"]["steps"]
    return [s["run"] for s in steps if "run" in s and "pytest" in s["run"]]


def _all_pytest_cmds() -> str:
    return "\n".join(_pytest_run_blocks())


# ---------------------------------------------------------------------------
# 1. 两棵树都在跑
# ---------------------------------------------------------------------------


def test_ci_跑_tests_树():
    assert any("pytest tests/" in c for c in _pytest_run_blocks()), "tests/ 不在 CI 里了"


def test_ci_跑_content_service_tests():
    """这条是本次的核心断言。

    之前 `pytest tests/` 是唯一的命令，于是 150 个 content-service 用例
    完全不在 CI 里 —— 而且这件事没有任何地方会报警。
    """
    assert any("content-service/tests" in c for c in _pytest_run_blocks()), (
        "content-service/tests/ 又从 CI 里消失了 —— " "这正是 150 个用例在 2026-10 之前腐烂的机制"
    )


def test_两棵树_必须分开跑():
    """不能把两棵树合并成一条 pytest 命令。

    content-service/tests/fetchers/ 目录名就叫 `fetchers`，与真实的
    content-service/fetchers 包同名。同一进程收集两棵树时，谁顶掉谁取决于
    sys.path 顺序，结果是 9 个模块 ModuleNotFoundError、整个 job 直接挂。

    这条不是为了规定写法，而是为了让人在「顺手合并一下」之前先看到这个坑。
    """
    for cmd in _pytest_run_blocks():
        roots = re.findall(r"pytest\s+([^\s\\]+)", cmd)
        tree_roots = {r for r in roots if r.rstrip("/") in ("tests", "content-service/tests")}
        assert (
            len(tree_roots) <= 1
        ), f"同一条命令里出现了多棵树 {tree_roots} —— 会撞 `fetchers` 同名包"


# ---------------------------------------------------------------------------
# 2. 排除依据在代码里，不在注释里
# ---------------------------------------------------------------------------


def test_marker_口径_只在_pyproject_定义一次():
    """workflow 里**不许**再写 `-m`：口径统一在 pyproject 的 addopts。

    ⚠️ 为什么这是硬要求（2026-10-25 实测的机制）：
    pytest 的 `-m` 是单值选项（`action="store"`）⇒ **命令行上的 `-m` 会覆盖
    addopts 里的**。以前 workflow 写 `-m "not network"`、本地什么都不写 ⇒
    CI 与本地的「跑哪些用例」是**两套口径**，只靠人记着；而 addopts 里的
    表达式会被命令行那一条**整个顶掉**（不是叠加）⇒ 加了 addopts 也一样不生效。

    ⇒ 口径写在一处（pyproject 的 addopts），workflow 与本地都不再写 `-m`；
    显式 opt-in 时两边都加 `-m <marker>`（命令行覆盖 addopts，语义一致）。
    """
    for cmd in _pytest_run_blocks():
        assert "-m " not in cmd, (
            "pytest 命令里又出现 -m：口径会与 pyproject 的 addopts 漂移（"
            f"命令行的 -m 会整个覆盖 addopts）。{cmd.strip()[:80]}"
        )

    text = PYPROJECT.read_text()
    ini = text.split("[tool.pytest.ini_options]", 1)
    assert len(ini) == 2, "找不到 [tool.pytest.ini_options] 段"
    section = ini[1].split("[tool.ruff]", 1)[0]

    assert "addopts" in section, "pyproject 里没有 addopts —— marker 口径没地方统一"
    # 用 tomllib 正经解析 TOML，不要用正则：addopts 的值里带嵌套引号
    # （'-m "not network and not integration"'），正则会在第一个引号处截断，
    # 把表达式读成 "-m " —— 断言就成了「永远不成立」或「永远成立」的假信号。
    import tomllib

    with PYPROJECT.open("rb") as fh:
        opts = tomllib.load(fh).get("tool", {}).get("pytest", {}).get("ini_options", {})
    expr = opts.get("addopts", "")
    assert "not network" in expr, f"addopts 没排除 network 用例：{expr!r}"
    assert "not integration" in expr, f"addopts 没排除 integration 用例：{expr!r}"


def test_live_service_用例_都打了_integration_标记():
    """需要活的服务、会真的建文章派任务的用例，必须打 `integration` 标记。

    为什么这条要单独断言：`integration` 标记是**默认排除**的唯一依据 ——
    漏打一个文件，它就会在每次全量 pytest 时真跑，然后：
      · 往 e2e 的共享 arq 队列里灌一个真蒸馏任务（真 LLM + 真 TTS，100~250s）；
      · 把**后面**任何一次集成测试的等待时间拖长
        （2026-10-25 实测：某个任务在队列里排了 1381 秒，测试等不到就误报超时）。

    ⇒ 「新增一个 live-service 用例文件」必须同时打标记，否则默认套件会变慢、
    变不稳，而且症状出现在**别的**测试上（极难定位）。
    """
    integration_dir = BACKEND_DIR / "tests" / "integration"
    files = sorted(p for p in integration_dir.glob("test_*.py"))
    assert files, "tests/integration/ 下一个测试文件都没有 —— 判据失效了"

    for path in files:
        src = path.read_text()
        has_marker = "pytest.mark.integration" in src
        # 本目录下的文件按约定必须显式声明；这里逐个点名，避免"目录里大部分
        # 打了就算过"这种会随新文件退化的判据。
        assert has_marker, (
            f"{path.name} 没打 pytest.mark.integration —— "
            "默认套件会真跑它（建文章 + 派蒸馏任务 + 拖慢后面的集成测试）。"
            "确实是纯单测的话，把文件挪出 tests/integration/。"
        )


def test_network_标记_已在_pyproject_注册():
    """未注册的 marker 只会告警、不会生效 —— 那就等于没排除。

    PytestUnknownMarkWarning 在日志里很容易被忽略过去。
    """
    text = PYPROJECT.read_text()
    ini = text.split("[tool.pytest.ini_options]", 1)
    assert len(ini) == 2, "找不到 [tool.pytest.ini_options] 段"
    section = ini[1].split("[tool.ruff]", 1)[0]

    assert "markers" in section, "pyproject 里没有注册 markers"
    assert (
        "network" in section
    ), "network marker 没注册 —— pytest 只会告警，-m 'not network' 形同虚设"
    assert (
        "integration" in section
    ), "integration marker 没注册 —— pytest 只会告警，-m 'not integration' 形同虚设"


def test_真实传输层_被封死():
    """content-service/tests 的 conftest 必须拦掉 httpx 的真实传输。

    这是「标记约定」背后的强制力。第一版这里写的是「扫描测试文件里有没有
    真实 URL」，实测立刻打脸：11 个文件被误报 —— 测试里用 example.com 当
    **假**域名是完全正常的写法，出现 URL 根本不是出网的证据。

    真正可靠的判据是行为而非文本：把 httpx 真实传输层换成抛异常，
    于是「谁真的出网了」由运行时回答，不需要猜。本目录下 140 个用例
    （ASGITransport / MockTransport）在封堵下全绿，正是这个保证的证据。
    """
    conftest = Path(__file__).resolve().parents[1] / "content-service" / "tests" / "conftest.py"
    src = conftest.read_text()

    assert "AsyncHTTPTransport" in src and "handle_async_request" in src, (
        "conftest 里没有封堵 httpx.AsyncHTTPTransport —— "
        "漏标记的出网用例会安静地去访问真实站点，把 CI 拖死"
    )
    assert (
        "HTTPTransport" in src and "handle_request" in src
    ), "同步传输层没封 —— 漏网的用例照样能出网"


def test_出网用例_打了_network_标记():
    """确认已知的那批真出网用例确实打了标记（而不是靠约定）。"""
    e2e = (
        Path(__file__).resolve().parents[1]
        / "content-service"
        / "tests"
        / "integration"
        / "test_fetch_e2e.py"
    )
    src = e2e.read_text()

    assert (
        "pytestmark = pytest.mark.network" in src
    ), "test_fetch_e2e.py 打的是 10 个真实站点，缺 network 标记会被 CI 跑"


def test_ci_不再有整目录_ignore():
    """`--ignore` 整目录是最容易让人失去信号的工具，必须归零。

    之前那 5 条 --ignore 里有 4 条早已失效（问题早修好了），却没人回来清理 ——
    它们只是在替 bug 兜底，同时让 bug 永远暴露不出来。
    """
    for cmd in _pytest_run_blocks():
        assert "--ignore" not in cmd, f"pytest 命令里又出现 --ignore：{cmd.strip()[:80]}"


# ---------------------------------------------------------------------------
# 3. 报告不能互相覆盖
# ---------------------------------------------------------------------------


def test_两棵树的报告_分别上传():
    """artifact 的 path 必须同时包含两个报告文件。

    只写一个的话，后跑的会覆盖先跑的，「只剩一棵树的结果」在界面上看起来
    和「两棵树都跑了」完全一样 —— 又是一个静默失败。
    """
    steps = _workflow()["jobs"]["pytest"]["steps"]
    upload = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")]
    assert upload, "找不到上传报告的步骤"

    path = upload[0]["with"]["path"]
    assert "pytest-report.xml" in path
    assert (
        "pytest-content-service-report.xml" in path
    ), "报告 artifact 只包含一棵树 —— 另一棵的结果会被覆盖掉"


# ---------------------------------------------------------------------------
# 4. 环境前提（顺手钉住，避免改 workflow 时踩空）
# ---------------------------------------------------------------------------


def test_ci_有跑_alembic():
    """content-service 的集成测要真表结构，迁移必须先跑。"""
    steps = _workflow()["jobs"]["pytest"]["steps"]
    runs = [s.get("run", "") for s in steps]
    assert any(
        "alembic upgrade head" in r for r in runs
    ), "CI 里没有 alembic upgrade head —— 新加的集成测会因表不存在而全红"


def test_ci_有设_pythonpath():
    """两棵树的测试都按 `stashbox.backend.*` 绝对包名导入，需要仓库父目录在 path 上。"""
    env = _workflow()["jobs"]["pytest"].get("env", {})
    assert (
        "PYTHONPATH" in env
    ), "CI env 里没有 PYTHONPATH —— content-service/tests 会 10 个模块收集失败"
