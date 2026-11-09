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
    from stashbox.backend.common import upstream_config

    calls = {"n": 0}

    async def _flaky(key):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"provider": "openai", "api_key": "sk-1", "base_url": "https://a"}
        raise RuntimeError("PG 抖动")

    monkeypatch.setattr(upstream_config, "get_config", _flaky)

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
    from stashbox.backend.common import upstream_config

    async def _boom(key):
        raise RuntimeError("PG 不可达")

    monkeypatch.setattr(upstream_config, "get_config", _boom)

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
    from stashbox.backend.common import upstream_config

    async def _ok(key):
        return {"provider": "openai", "api_key": "sk-1"}

    monkeypatch.setattr(upstream_config, "get_config", _ok)

    assert last_good("llm") is None
    await load_llm()
    assert last_good("llm")["api_key"] == "sk-1"


@pytest.mark.asyncio
async def test_行不存在时用代码默认值(monkeypatch):
    """表里没这一行 = 明确"没配" ⇒ 代码默认值，如实记成 defaults 来源。"""
    from stashbox.backend.common import upstream_config

    async def _none(key):
        return None

    monkeypatch.setattr(upstream_config, "get_config", _none)

    cfg, source = await load_llm()
    assert source == "defaults"
    assert cfg["api_key"] == ""


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_启动自检报告缺配字段(monkeypatch):
    """自检要点名"缺哪个字段"，而不是只说"有问题"。"""
    from stashbox.backend.common import upstream_config

    async def _no_key(key):
        if key == "llm":
            return {"provider": "openai", "base_url": "https://a"}
        return {"provider": "openai", "openai_api_key": "sk", "openai_base_url": "https://b"}

    monkeypatch.setattr(upstream_config, "get_config", _no_key)

    report = await diagnose()
    assert report["llm"]["ok"] is False
    assert "api_key" in report["llm"]["missing"]
    assert report["tts"]["ok"] is True


@pytest.mark.asyncio
async def test_启动自检在配置不可用时报告不可用(monkeypatch):
    from stashbox.backend.common import upstream_config

    async def _boom(key):
        raise RuntimeError("PG 不可达")

    monkeypatch.setattr(upstream_config, "get_config", _boom)

    report = await diagnose()
    assert report["llm"]["ok"] is False
    assert report["llm"]["source"] == "unavailable"
