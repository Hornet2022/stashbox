"""上游配置的**单一真相源**契约（只认 DB）—— 2026-11-10。

## 这组用例钉住什么

2026-11-09 的事故链：

1. 管理后台把 LLM 切到新端点 → DB 写对了；
2. 但 env 里**残留着已下线的端点**（旧 provider 的 base_url / key）；
3. 某次 DB 读失败（Redis/PG 抖动）→ 代码**静默回落 env**；
4. 请求打到已下线的端点 → 401 / 超时，**日志里看不出"配置没读到"**。

根因不是某一行代码写错，而是**配置有两个真相源**（DB 与 env），
且优先级是**逐字段回落**。

所以这里不只测"行为对不对"，还用**静态护栏**（扫源码 AST）钉住
"身份类字段不许再从 env 读" —— 因为这类回归**没有任何行为测试能发现**：
env 兜底只在"参数没传"时才触发，而正常路径总是传值。

## 三层护栏

1. **静态**：身份类 env 变量不许出现在运行时配置路径上（AST 扫描，零成本）。
2. **行为**：DB 读失败 → last-good；没有 last-good → 抛明确异常（不静默降级）。
3. **行为**：DB 缺字段 → 代码默认值（不是 env）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from stashbox.backend.common.upstream_config import (
    LLM_CODE_DEFAULTS,
    TTS_CODE_DEFAULTS,
    UpstreamConfigUnavailable,
    current_llm,
    current_tts,
    diagnose,
    last_good,
    load_llm,
    reset_for_tests,
    resolve_llm,
    resolve_tts,
    source_of,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]

# ── 身份类 env 变量：决定"打哪个上游 / 用什么凭据 / 用哪个模型 / 什么音色" ──
#
# 这些**只允许**出现在 deploy/seed_upstream_config.py（一次性播种）
# 与文档里；运行时路径上出现 = 第二个真相源复活。
IDENTITY_ENV_VARS = frozenset(
    {
        # LLM
        "LLM_PROVIDER",
        "LLM_TIMEOUT",
        "LLM_MAX_RETRIES",
        "LLM_CONNECT_TIMEOUT",
        "OPENAI_LLM_API_KEY",
        "OPENAI_LLM_MODEL",
        "OPENAI_LLM_BASE_URL",
        "QWEN_VL_API_KEY",
        "QWEN_VL_MODEL",
        "QWEN_VL_BASE_URL",
        "DASHSCOPE_API_KEY",
        # TTS
        "TTS_PROVIDER",
        "OPENAI_TTS_API_KEY",
        "OPENAI_TTS_MODEL",
        "OPENAI_TTS_VOICE",
        "OPENAI_TTS_INSTRUCTIONS",
        "OPENAI_TTS_LANGUAGE",
        "OPENAI_BASE_URL",
        "DOUBAO_TTS_API_KEY",
        "DOUBAO_TTS_TOKEN",
        "DOUBAO_TTS_APP_ID",
        "DOUBAO_TTS_VOICE",
        "DOUBAO_TTS_RESOURCE_ID",
        "EDGE_TTS_VOICE",
        "TTS_LOCAL_VOICE",
        "INDEXTTS_BASE_URL",
        "INDEXTTS_MODEL",
        "INDEXTTS_REF_AUDIO",
        "INDEXTTS_REF_TEXT",
        "INDEXTTS_SPEAKER",
        "INDEXTTS_INSTRUCT",
        "INDEXTTS_TEMPERATURE",
        "INDEXTTS_TOP_P",
        # OCR
        "STASHBOX_OCR_MODEL",
    }
)

# 运行时配置路径：这些文件里的 env 回落 = 第二真相源。
# （播种脚本在 deploy/ 下，不在此列 —— 它**就是**那个一次性 env 消费者。）
RUNTIME_CONFIG_FILES = (
    "common/upstream_config.py",
    "ai-service/llm/factory.py",
    "ai-service/llm/openai.py",
    "ai-service/llm/qwen_vl.py",
    "app/services/llm/__init__.py",
    "app/services/llm/openai.py",
    "app/services/llm/qwen.py",
    "app/services/tts/__init__.py",
    "app/services/tts/openai.py",
    "app/services/tts/doubao.py",
    "app/services/tts/indextts.py",
    "app/services/tts/local.py",
    "app/services/tts/edge.py",
    "content-service/fetchers/ocr.py",
    "content-service/admin_router.py",
)


def _env_vars_read(path: Path) -> set[str]:
    """扫一个文件里所有 `os.getenv("X")` / `os.environ.get("X")` 的 X。

    用 AST 而不是正则：正则会漏掉 `os.getenv(var_name)` 这种间接写法，
    也会被注释/字符串里的同名字样误伤。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # os.getenv(...) / os.environ.get(...)
        is_getenv = (
            isinstance(func, ast.Attribute)
            and func.attr == "getenv"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        )
        is_environ_get = (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
            and func.value.attr == "environ"
        )
        if not (is_getenv or is_environ_get):
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


# ---------------------------------------------------------------------------
# 护栏 1（静态）：身份类 env 不得出现在运行时配置路径
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rel_path", RUNTIME_CONFIG_FILES)
def test_运行时配置路径不读身份类env(rel_path):
    """身份类 env 变量一旦回到运行时路径，第二个真相源就复活了。

    这条护栏的价值：env 回落**只在参数没传时**触发 ⇒ 行为测试抓不到它
    （正常路径总是传值）。只有静态扫描能抓住。
    """
    path = BACKEND_DIR / rel_path
    assert path.is_file(), f"{rel_path} 不存在（护栏本身失效了）"

    offenders = _env_vars_read(path) & IDENTITY_ENV_VARS
    assert not offenders, (
        f"{rel_path} 又开始读身份类 env 变量 {sorted(offenders)} —— "
        "上游配置只认 DB（system_config 表），env 只是播种时用一次。"
        "请改成从上游配置解析结果取值。"
    )


def test_唯一真相源模块自身不读env():
    """`common/upstream_config.py` 是唯一真相源 —— 它自己绝不能读 env。

    这是整个契约的根：它一旦读 env，所有下游都会跟着有两个真相源。
    """
    path = BACKEND_DIR / "common/upstream_config.py"
    offenders = _env_vars_read(path) & IDENTITY_ENV_VARS
    assert not offenders, f"upstream_config.py 读了身份类 env {sorted(offenders)}"


def test_唯一真相源不得绑死被测函数():
    """`upstream_config` 不得用 `from ... import get_config` 把函数绑死。

    ⚠️ 这条护栏来自一次真实的翻车（2026-11-10）：
    `from ...system_config import get_config` 会在 import 时把函数**绑死**在本模块，
    于是 `monkeypatch.setattr(system_config, "get_config", ...)` **打不到这里**
    （pytest 的通行规则：patch where it's looked up）。

    后果不是"测试红了"，而是**测试打桩打歪了** ⇒ 用例读的是真 DB ⇒
    结果与环境相关 ⇒ 变成"偶尔红"的 flaky 用例，且没人知道它已经不测它想测的东西了。

    实测：tier-config 的回归用例打桩了 `system_config.get_config`，
    而 upstream_config 没被打到 ⇒ 用例读真 DB ⇒ 收敛改动一落地就红。

    治本：用**模块限定**调用（`system_config.get_config(...)`）。
    """
    import ast

    path = BACKEND_DIR / "common/upstream_config.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    bound: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                # 常量（KEY_*）用 from-import 没问题；被调用的函数不行
                if alias.name in {"get_config", "get_config_row", "set_config"}:
                    bound.append(f"{node.module}.{alias.name}")
    assert not bound, (
        f"upstream_config.py 绑死了 {bound} —— "
        "改成模块限定调用 `system_config.get_config(...)`，"
        "否则 monkeypatch 打桩打不到这里，测试会读真 DB 变成 flaky。"
    )

    # 并且确实用的是模块限定调用
    src = path.read_text(encoding="utf-8")
    assert "system_config.get_config(" in src, "upstream_config.py 应该用模块限定调用读配置"


def test_上游配置模块不导入os():
    """更狠的一条：`upstream_config.py` 连 `os` 都不该导入。

    导入 os 本身无害，但"这个模块需要 os"是一个**信号** ——
    说明有人打算在这里读 env。挡住这个信号，比事后 review 便宜。
    """
    tree = ast.parse((BACKEND_DIR / "common/upstream_config.py").read_text(encoding="utf-8"))
    imported_os = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_os |= any(a.name == "os" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_os |= node.module == "os"
    assert not imported_os, "upstream_config.py 不该 import os（它不需要读 env）"


# ---------------------------------------------------------------------------
# 护栏 2（行为）：DB 读失败 → last-good；没有 last-good → 抛明确异常
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_cache():
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.mark.asyncio
async def test_DB读失败时用last_good(monkeypatch):
    """DB 读失败 → 用本进程上次成功读到的配置（不静默换端点）。"""
    from stashbox.backend.common import system_config as system_config_mod

    calls = {"n": 0}

    async def _flaky(key):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"provider": "openai", "api_key": "sk-1", "base_url": "https://a"}
        raise RuntimeError("PG 抖动")

    monkeypatch.setattr(system_config_mod, "get_config", _flaky)

    cfg, source = await load_llm()
    assert source == "db"
    assert cfg["api_key"] == "sk-1"

    # 第二次读失败 → last-good
    cfg2, source2 = await load_llm()
    assert source2 == "last_good"
    assert cfg2["api_key"] == "sk-1"


@pytest.mark.asyncio
async def test_DB读失败且无last_good时抛明确异常(monkeypatch):
    """DB 读失败 + 本进程从未成功读过 → 抛明确异常，**不**静默用 env。

    这条是整个收敛的核心：以前这里是"静默回落 env"，而 env 里残留着
    **已下线的端点** ⇒ 请求打过去，症状是 401/超时，**看不出是配置没读到**。
    """
    from stashbox.backend.common import system_config as system_config_mod

    async def _boom(key):
        raise RuntimeError("PG 不可达")

    monkeypatch.setattr(system_config_mod, "get_config", _boom)

    with pytest.raises(UpstreamConfigUnavailable) as exc:
        await load_llm()
    # 异常里必须**点名**怎么修，否则运维只能猜
    msg = str(exc.value)
    assert "seed_upstream_config" in msg
    assert exc.value.kind == "config_unavailable"


@pytest.mark.asyncio
async def test_同步读取无缓存时抛明确异常():
    """`current_llm()` 在没有缓存时抛异常，不返回任何 env 兜底值。"""
    with pytest.raises(UpstreamConfigUnavailable):
        current_llm()
    with pytest.raises(UpstreamConfigUnavailable):
        current_tts()


# ---------------------------------------------------------------------------
# 护栏 3（行为）：DB 缺字段 → 代码默认值（不是 env）
# ---------------------------------------------------------------------------
def test_DB缺字段用代码默认值(monkeypatch):
    """DB 行里没有的字段 → 代码默认值。"""
    monkeypatch.setenv("OPENAI_LLM_API_KEY", "sk-from-env")
    monkeypatch.setenv("OPENAI_LLM_MODEL", "model-from-env")

    cfg = resolve_llm({"provider": "openai"})

    assert cfg["api_key"] == ""  # **不是** env 里的 sk-from-env
    assert cfg["model"] == LLM_CODE_DEFAULTS["model"]  # **不是** model-from-env
    assert cfg["timeout"] == LLM_CODE_DEFAULTS["timeout"]


def test_DB空串等同没配(monkeypatch):
    """DB 里的空串 = 没配（admin 的"用户没重新输入"约定）→ 代码默认值。"""
    monkeypatch.setenv("OPENAI_LLM_API_KEY", "sk-from-env")

    cfg = resolve_llm({"provider": "openai", "api_key": "", "model": ""})

    assert cfg["api_key"] == ""
    assert cfg["model"] == LLM_CODE_DEFAULTS["model"]


def test_TTS缺字段用代码默认值(monkeypatch):
    """TTS 侧同理。"""
    monkeypatch.setenv("OPENAI_TTS_API_KEY", "sk-from-env")
    monkeypatch.setenv("OPENAI_TTS_MODEL", "model-from-env")

    cfg = resolve_tts({"provider": "openai"})

    assert cfg["openai_api_key"] == ""
    assert cfg["openai_model"] == TTS_CODE_DEFAULTS["openai_model"]


# ---------------------------------------------------------------------------
# 规范化语义（通用字段 ↔ provider 专属字段）
# ---------------------------------------------------------------------------
def test_通用字段同步到provider专属字段():
    """管理后台存的是通用字段 → 必须同步到 provider 专属字段。

    两层 client 抽象层各读各的专属字段，映射只在这里做一次。
    """
    cfg = resolve_llm(
        {
            "provider": "openai",
            "model": "DeepSeek-V4-Flash-0731",
            "api_key": "sk_x",
            "base_url": "https://hub.tsingfly.com/v1",
        }
    )
    assert cfg["openai_llm_model"] == "DeepSeek-V4-Flash-0731"
    assert cfg["openai_llm_api_key"] == "sk_x"
    assert cfg["openai_llm_base_url"] == "https://hub.tsingfly.com/v1"


def test_qwen_vl也同步():
    cfg = resolve_llm(
        {
            "provider": "qwen_vl",
            "model": "qwen-vl-max",
            "api_key": "qk",
            "base_url": "https://token-plan.example/v1",
        }
    )
    assert cfg["qwen_vl_model"] == "qwen-vl-max"
    assert cfg["qwen_vl_api_key"] == "qk"
    assert cfg["qwen_vl_base_url"] == "https://token-plan.example/v1"


def test_未知provider清空专属凭据():
    """未知 provider ⇒ 专属凭据清空 ⇒ 建 client 时报"不支持的 provider"。

    不能悄悄用别的 provider 的凭据去打。
    """
    cfg = resolve_llm({"provider": "azure_unsupported", "api_key": "sk_x"})
    assert cfg["openai_llm_api_key"] == ""
    assert cfg["qwen_vl_api_key"] == ""
    assert cfg["provider"] == "azure_unsupported"


def test_调用预算与OCR模型从DB读():
    """timeout / max_retries / connect_timeout / ocr_model 都落 DB。"""
    cfg = resolve_llm(
        {
            "provider": "openai",
            "timeout": 120,
            "max_retries": 2,
            "connect_timeout": 10,
            "ocr_model": "Qwen/Qwen3-VL-32B-Instruct",
        }
    )
    assert cfg["timeout"] == 120
    assert cfg["max_retries"] == 2
    assert cfg["connect_timeout"] == 10
    assert cfg["ocr_model"] == "Qwen/Qwen3-VL-32B-Instruct"


def test_超时预算零值不被当成没配():
    """timeout=0 是"显式设成 0"，不是"没配"（用 _is_set 判空会误伤）。"""
    cfg = resolve_llm({"provider": "openai", "timeout": 0})
    assert cfg["timeout"] == 0


# ---------------------------------------------------------------------------
# last-good 缓存
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_last_good记录最后一次成功读取(monkeypatch):
    from stashbox.backend.common import system_config as system_config_mod

    async def _ok(key):
        return {"provider": "openai", "api_key": "sk-1"}

    monkeypatch.setattr(system_config_mod, "get_config", _ok)

    assert last_good("llm") is None
    await load_llm()
    assert last_good("llm")["api_key"] == "sk-1"


@pytest.mark.asyncio
async def test_行不存在时运行期点名不静默用默认值(monkeypatch):
    """表里没这一行 ⇒ 运行期**必须点名**，不许拿代码默认值去打上游。

    ⚠️ 这条契约在 2026-11-10 被**收紧**了（原先是"用代码默认值"）。

    为什么收紧：代码默认值是 `api.openai.com` + **空 api_key** ⇒ 请求打过去
    症状是 401 / 连不上，排查方向会被带偏到"key 无效"或"网络问题"，
    而真相是"**压根没配**"。这与 2026-11-09 的 env 回落是同一个病：
    **配置没读到，却伪装成上游的问题**。

    区别在于：admin 回显仍要能显示"当前是默认值"（那是 UI 语义，不是运行期语义），
    所以有 `peek_llm` / `peek_tts` 走宽松路径。
    """
    from stashbox.backend.common import system_config as system_config_mod

    async def _none(key):
        return None

    monkeypatch.setattr(system_config_mod, "get_config", _none)

    with pytest.raises(UpstreamConfigUnavailable) as exc:
        await load_llm()
    # 异常里必须点名"没配"，让人知道该去后台补，而不是去查 key / 查网络
    msg = str(exc.value)
    assert "没有配" in msg
    assert exc.value.kind == "config_missing"


@pytest.mark.asyncio
async def test_宽松读取行不存在不抛(monkeypatch):
    """`peek_llm` 是给"纯回显"用的：行不存在 ⇒ 返回默认值，**不抛**。

    音色兜底解析（`tts_voice_service`）走的就是这条路 ——
    它只是"读一下当前配置长什么样"，不该因为配置没配就打断整条链路。
    """
    from stashbox.backend.common import system_config as system_config_mod
    from stashbox.backend.common import upstream_config

    async def _none(key):
        return None

    monkeypatch.setattr(system_config_mod, "get_config", _none)

    cfg, source = await upstream_config.peek_llm()
    assert source == "defaults"
    assert cfg["api_key"] == ""


@pytest.mark.asyncio
async def test_配置不完整时点名缺哪个字段(monkeypatch):
    """DB 行存在但 api_key 是空串 ⇒ 点名"缺 api_key"，不许拿残缺配置去试。

    症状会是 401（看起来像"key 无效"），而真相是"没存进去"。
    """
    from stashbox.backend.common import system_config as system_config_mod

    async def _no_key(key):
        return {"provider": "openai", "base_url": "https://a", "api_key": ""}

    monkeypatch.setattr(system_config_mod, "get_config", _no_key)

    with pytest.raises(UpstreamConfigUnavailable) as exc:
        await load_llm()
    assert "api_key" in str(exc.value)
    assert exc.value.kind == "config_missing"


@pytest.mark.asyncio
async def test_行不存在不污染last_good(monkeypatch):
    """行不存在时的代码默认值**不许**被记成 last-good。

    否则下一次 DB 抖动时，`last_good` 里躺的是"空 key + api.openai.com"，
    又变成同一种伪装（症状是 401，真相是压根没配）。

    ⚠️ 这条护栏要覆盖**宽松路径**（`peek_*`）：strict 路径在 `_remember` **之前**
    就抛异常了，污染压根不会发生 —— 护在 strict 上是"抓一个不存在的缺陷"，
    等于没有牙齿（实测：这样写的护栏，变异检验**没红**）。

    真正会污染的是 `peek_llm`：它正常返回 ⇒ 走到 `_remember` ⇒ 记下默认值。
    """
    from stashbox.backend.common import system_config as system_config_mod
    from stashbox.backend.common import upstream_config

    async def _none(key):
        return None

    monkeypatch.setattr(system_config_mod, "get_config", _none)

    cfg, source = await upstream_config.peek_llm()
    assert source == "defaults"
    assert last_good("llm") is None, "行不存在不该产生 last-good"


@pytest.mark.asyncio
async def test_严格路径行不存在时也不记last_good(monkeypatch):
    """strict 路径同样不许留下 last-good（它先抛异常，但护栏要钉住这个前提）。

    若将来有人把"行不存在"改成"返回默认值"（回到老契约），
    这条会立刻抓到 last-good 被污染。
    """
    from stashbox.backend.common import system_config as system_config_mod

    async def _none(key):
        return None

    monkeypatch.setattr(system_config_mod, "get_config", _none)

    with pytest.raises(UpstreamConfigUnavailable):
        await load_llm()
    assert last_good("llm") is None


# ---------------------------------------------------------------------------
# source 判据：全仓只有一份
# ---------------------------------------------------------------------------
def test_source判据全仓唯一():
    """`source` 的判定（db / defaults）必须只有**一份实现**。

    曾经有两处：admin 端点用 truthiness（`if stored`），
    upstream_config 用 `is not None` ⇒ DB 里存的是空对象 `{}` 时，
    admin 说"defaults"、运行期说"db" ⇒ **同一个状态两个来源标签**。

    判据：判"行在不在"，不判"行空不空" —— `{}` 也是 db（行确实在）。
    """
    assert source_of(None) == "defaults"
    assert source_of({}) == "db"
    assert source_of({"provider": "openai"}) == "db"


def test_admin端点用同一个source判据():
    """admin 端点不许自己判 source —— 必须用 upstream_config.source_of。"""
    import ast

    path = BACKEND_DIR / "content-service" / "admin_router.py"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 找出所有 source=... 的来源表达式里有没有 "db" if ... else "defaults" 这种内联判定
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.IfExp):
            test_src = ast.unparse(node.test)
            body_src = ast.unparse(node.body)
            orelse_src = ast.unparse(node.orelse)
            if body_src == "'db'" and orelse_src == "'defaults'":
                offenders.append(test_src)
    assert not offenders, (
        f"admin_router.py 里内联判了 source（{offenders}）—— "
        "source 的判据必须在 common/upstream_config.source_of 一处。"
    )
    # 并且它确实导入并用了 source_of
    assert "source_of" in src, "admin_router.py 应该用 upstream_config.source_of"


# ---------------------------------------------------------------------------
# 护栏：消费方不许绕过统一解析器手搓 DB 原始行
# ---------------------------------------------------------------------------
#
# 为什么这条重要（2026-11-10 实测）：
#   `tier_router` / `ai-service/admin_router` 原先直接 `get_config(KEY_LLM) or {}`
#   再自己取 `provider` / `model` / `base_url` ⇒ **第二份解析实现**。
#   DB 里若存的是 provider 专属字段（`openai_llm_model`），原始行里就没有通用的
#   `model` ⇒ 拿到空 ⇒ **静默不跟随**当前模型 ⇒ 蒸馏时用硬编码 gpt-4o 打新端点
#   ⇒ 404 UnsupportedModel。症状是"模型不存在"，真相是"读配置的地方抄了近路"。
#
# 这类缺陷**没有任何行为测试能发现**（正常路径两处读到的值一样），
# 只有静态扫描能抓住 —— 与"env 回落"护栏同族。
CONSUMERS_THAT_MUST_USE_RESOLVER = (
    "ai-service/distill/tier_router.py",
    "ai-service/admin_router.py",
    "content-service/admin_router.py",
)


@pytest.mark.parametrize("rel_path", CONSUMERS_THAT_MUST_USE_RESOLVER)
def test_消费方必须走统一解析器(rel_path):
    """这些文件读上游配置**必须**走统一解析器，不许自己拿原始行。

    统一解析器（`upstream_config.resolve_llm` / `peek_llm`）是唯一入口 ——
    它知道"通用字段 ↔ provider 专属字段"的映射，手搓就拿不到那层映射。

    ⚠️ **例外**：**写路径**的"合并已存值"必须读原始行 ——
    PUT 端点要把用户**没改**的字段原样保留下来，解析器给不出"原样"。
    这是读原始行的**正当**用途，不是平行实现。

    怎么区分？本护栏只盯**读来建 client / 取生效值**的用法：
    取出来的值紧接着被 `.get("api_key")` / `.get("model")` 这类**取值**用，
    就是"读配置"；而 `dict(... or {})` 这种"整体搬走再改写"是写路径。

    ⚠️ 护栏要**双向**钉：
      · 反向（不许出现 `get_config(KEY_LLM)`）—— 挡住"手搓原始行"；
      · 正向（必须出现 `peek_llm` / `resolve_llm`）—— 挡住"用别的方式绕过"，
        也挡住"把整段读取删掉了"这种让反向检查变得空洞的改动。
    """
    import ast

    path = BACKEND_DIR / rel_path
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # ── 反向：读原始行（写路径除外）──────────────────────────────
    parent_of: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_of[id(child)] = node

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get_config"):
            continue
        arg_src = ast.unparse(node.args[0]) if node.args else ""
        if arg_src not in {"KEY_LLM", '"llm"', "system_config.KEY_LLM"}:
            continue

        # 写路径：结果被 dict(...) 包起来 ⇒ 整体搬走再改写 ⇒ 允许。
        # 形态：`dict(await get_config(...) or {})` ⇒ get_config 的祖先链是
        #   Call(get_config) → Await → BoolOp → Call(dict)
        # 所以要**沿祖先链往上找**，看有没有 dict(...) 这一层。
        cur: ast.AST | None = node
        is_write_path = False
        for _ in range(6):  # 祖先链最多看 6 层，足够覆盖上面那个形态
            cur = parent_of.get(id(cur)) if cur is not None else None
            if cur is None:
                break
            if isinstance(cur, ast.Call) and isinstance(cur.func, ast.Name):
                if cur.func.id == "dict":
                    is_write_path = True
                    break
        if is_write_path:
            continue

        # 其余都算"读配置" ⇒ 必须走解析器
        offenders.append(f"get_config({arg_src})")

    assert not offenders, (
        f"{rel_path} 直接读了 DB 原始行 {offenders} —— "
        "请改用 upstream_config 的统一解析器（resolve_llm / peek_llm），"
        "否则会拿到第二份解析实现，provider 专属字段读不到 ⇒ 静默走偏。"
    )

    # ── 正向：必须真的用了统一解析器 ────────────────────────────
    #
    # ⚠️ 必须用 **AST 调用检查**，不能用子串匹配！
    # ��串会被 **docstring / 注释**喂饱：这些文件里写着"用 `peek_llm`"的说明，
    # 于是即使代码里的调用被换掉，检查照样绿 —— 护栏变成"看着有、其实没有"。
    # （实测：这样写的护栏，变异检验**没红**。）
    resolver_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        leaf = name.rsplit(".", 1)[-1]
        if leaf in {"peek_llm", "peek_tts", "resolve_llm", "resolve_tts", "resolve_config"}:
            resolver_calls.append(name)

    assert resolver_calls, (
        f"{rel_path} 里找不到统一解析器的**调用** —— "
        "它必须通过 upstream_config（peek_llm / resolve_llm / resolve_config）读上游配置。"
        "（反向检查单独存在会有漏洞：把读取整段删掉，反向检查就变成空洞的。）"
        "注意：docstring 里提到名字不算数，必须是真实的函数调用。"
    )


# ---------------------------------------------------------------------------
# 调用预算：DB 里的值必须对**所有** client 构造路径生效
# ---------------------------------------------------------------------------
def test_app_services_llm透传预算字段():
    """`app/services/llm.build_client` 必须把 DB 的预算字段传给 client。

    ⚠️ 2026-11-10 发现的缺口：这层原先只传 api_key/model/base_url
    ⇒ timeout 永远是构造器默认的 60s、max_retries 永远是 3。
    而同一个 DB 行里的预算字段，ai-service 那层（蒸馏）却是在用的
    ⇒ **后台改 timeout 只对一半链路生效**。

    这是"配置收敛到 DB"最容易漏的一环：字段进了 DB，但消费方没全接上。
    """
    from stashbox.backend.app.services.llm import build_client

    cfg = resolve_llm(
        {
            "provider": "openai",
            "model": "m",
            "api_key": "sk",
            "base_url": "https://a",
            "timeout": 120,
            "max_retries": 2,
            "connect_timeout": 10,
        }
    )
    client = build_client(cfg)
    assert client._client.timeout.read == 120
    assert client.max_retries == 2
    # 连接超时独立于读超时（2026-11-09 事故教训）
    assert client._client.timeout.connect == 10


def test_app_services_llm预算缺省时用代码默认值():
    """DB 里没配预算 ⇒ 用**代码默认值**，不是 client 构造器的魔法数。"""
    from stashbox.backend.app.services.llm import build_client

    cfg = resolve_llm({"provider": "openai", "api_key": "sk", "base_url": "https://a"})
    client = build_client(cfg)
    assert client._client.timeout.read == LLM_CODE_DEFAULTS["timeout"]
    assert client.max_retries == LLM_CODE_DEFAULTS["max_retries"]
    assert client._client.timeout.connect == LLM_CODE_DEFAULTS["connect_timeout"]


@pytest.mark.parametrize(
    "rel_path",
    [
        "app/services/llm/openai.py",
        "app/services/llm/qwen.py",
        "app/services/tts/openai.py",
        "app/services/tts/doubao.py",
        "app/services/tts/indextts.py",
    ],
)
def test_所有上游client都有独立连接超时(rel_path):
    """每个上游 client 的 httpx timeout 都必须**拆开连接与读**。

    2026-11-09 事故：上游整机不可达（TCP 黑洞）⇒ httpx 拿读超时去等连接，
    每次尝试白等满读超时（LLM 60s / TTS 300s）⇒ 单篇等 30 分钟。

    静态护栏的价值：这类缺陷**只有真跑才暴露**，行为测试抓不到
    （正常路径连接耗时是毫秒级，拆与不拆都能通）。
    """
    import ast

    path = BACKEND_DIR / rel_path
    tree = ast.parse(path.read_text(encoding="utf-8"))

    timeout_kwargs: list[ast.keyword] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "AsyncClient"
                and isinstance(func.value, ast.Name)
                and func.value.id == "httpx"
            ):
                for kw in node.keywords:
                    if kw.arg == "timeout":
                        timeout_kwargs.append(kw)

    assert timeout_kwargs, f"{rel_path} 里没有找到 httpx.AsyncClient(timeout=...)"
    for kw in timeout_kwargs:
        src = ast.unparse(kw.value)
        assert "Timeout(" in src, (
            f"{rel_path} 的 httpx timeout 仍是单个数（{src}）—— "
            "连接超时必须独立于读超时（2026-11-09 事故教训）。"
        )
        assert "connect=" in src, f"{rel_path} 的 httpx.Timeout 缺 connect=（{src}）"


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_启动自检报告缺配字段(monkeypatch):
    """自检要点名"缺哪个字段"，而不是只说"有问题"。"""
    from stashbox.backend.common import system_config as system_config_mod

    async def _no_key(key):
        if key == "llm":
            return {"provider": "openai", "base_url": "https://a"}
        return {"provider": "openai", "openai_api_key": "sk", "openai_base_url": "https://b"}

    monkeypatch.setattr(system_config_mod, "get_config", _no_key)

    report = await diagnose()
    assert report["llm"]["ok"] is False
    assert "api_key" in report["llm"]["missing"]
    assert report["tts"]["ok"] is True


@pytest.mark.asyncio
async def test_启动自检在配置不可用时报告不可用(monkeypatch):
    from stashbox.backend.common import system_config as system_config_mod

    async def _boom(key):
        raise RuntimeError("PG 不可达")

    monkeypatch.setattr(system_config_mod, "get_config", _boom)

    report = await diagnose()
    assert report["llm"]["ok"] is False
    assert report["llm"]["source"] == "unavailable"
