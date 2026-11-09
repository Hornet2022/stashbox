"""上游（LLM / TTS）运行时配置的**唯一真相源**：DB（`system_config` 表）。

## 为什么必须收敛（2026-11-10）

事故链长这样：

1. 管理后台把 LLM 切到新端点 → DB 写对了；
2. 但 env 里**残留着已下线的端点**（旧 provider 的 base_url / key）；
3. 某次 DB 读失败（Redis/PG 抖动）→ 代码**静默回落 env**；
4. 请求打到已下线的端点 → 401 / 超时，而且**日志里看不出"配置没读到"**。

根因不是某一行代码写错，而是**配置有两个真相源**（DB 与 env），
且优先级是**逐字段回落** —— 只要 env 里那个字段非空，DB 的缺失就会被 env 顶掉。
于是"哪个端点生效"这件事取决于**哪一层先失败**，而不是取决于配置本身。

## 收敛后的契约

1. **运行时配置只从 DB 读**（`system_config` 表的 `llm` / `tts` 两行）。
2. DB 里缺某个字段 → 用**本模块的代码默认值**
   （`LLM_CODE_DEFAULTS` / `TTS_CODE_DEFAULTS`），**不回落 env**。
   代码默认值是"没配"的显式表达，不是第二个真相源。
3. DB 读失败（PG 抛异常）→ 用**本进程最后一次成功读到的配置**（last-good）；
   本进程从未成功读过 → 抛 `UpstreamConfigUnavailable`（明确可操作），
   **不静默降级**。
4. env **只在部署播种时用一次**：`backend/deploy/seed_upstream_config.py`
   把 env 里的上游身份写进 DB；播种之后 env 就可以删掉。

## 边界：什么还留在 env

只承载**传输层 / 实现细节**、不决定"打哪个上游、用什么凭据"的参数：
分块长度、并发度、段间静音、ffmpeg/ffprobe 路径、音频目录、熔断阈值等。

凡是**管理后台能配的**（provider / 端点 / 凭据 / 模型 / 音色 /
LLM 的超时与重试预算），一律**只认 DB** —— 见 `tests/common/test_upstream_config.py`
里的静态护栏（改动会让护栏变红）。

## 与两套 LLM 抽象层的关系

`ai-service/llm/`（蒸馏用，接口是 ChatRequest→ChatResponse）与
`app/services/llm/`（content-service 用，接口是 prompt→str）本来是**两个独立
client 抽象层**，但**配置源必须只有一份** —— 两层都读本模块产出的同一份规范化配置，
"通用字段 ↔ provider 专属字段"的映射也**只在这里实现一次**
（曾经 factory.py 与 app/services/llm 各抄了一份，必然漂移）。
"""

from __future__ import annotations

from typing import Any

from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.system_config import KEY_LLM, KEY_TTS, get_config

log = get_logger(__name__)

# ── 代码默认值（DB 缺字段时用；**不是 env**）──────────────────────────────
#
# 为什么是"缺配置"的显式表达而不是另一个真相源：
#   · 它是**代码里写死**的，评审时看得见、不会被人偷偷改；
#   · 它不随部署环境变化 ⇒ 同样的 DB 在任何机器上解析结果一致；
#   · 关键字段（api_key）默认空 ⇒ 没配置时**大声报错**，而不是打到某个默认端点。
LLM_CODE_DEFAULTS: dict[str, Any] = {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "api_key": "",
    "base_url": "https://api.openai.com/v1",
    # 调用预算（管理后台可改，落 DB；这里只是"没配"时的兜底）
    "timeout": 60.0,
    "max_retries": 3,
    "connect_timeout": 10.0,
    # OCR 复用 LLM 的端点与凭据，但用**视觉模型**（默认 Qwen3-VL）
    "ocr_model": "Qwen/Qwen3-VL-32B-Instruct",
}

TTS_CODE_DEFAULTS: dict[str, Any] = {
    "provider": "indextts",
    # edge provider（免费无 key）
    "edge_voice": "zh-CN-XiaoxiaoNeural",
    # openai 协议（火山方舟 / OpenAI / 各类 OpenAI 兼容网关）
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_model": "tts-1",
    "openai_voice": "alloy",
    "openai_instructions": "",
    "openai_language": "",
    "openai_ref_audio": "",
    "openai_ref_text": "",
    # doubao（火山方舟 Coding Plan）
    "doubao_api_key": "",
    "doubao_token": "",
    "doubao_app_id": "",
    "doubao_voice": "BV001_streaming",
    "doubao_resource_id": "seed-tts-2.0",
    # local（macOS say + ffmpeg）
    "local_voice": "Tingting",
    "ffmpeg_bin": "/opt/homebrew/bin/ffmpeg",
    # indextts（自建 mlx-audio /v1/audio/speech + ref_audio 零样本克隆）
    "indextts_base_url": "http://127.0.0.1:8010/v1",
    "indextts_model": "Qwen3-TTS-12Hz-0.6B-Base-bf16",
    "indextts_ref_audio": "",
    "indextts_ref_text": "",
    "indextts_speaker": "",
    "indextts_instruct": "",
    "indextts_temperature": 0.7,
    "indextts_top_p": 0.9,
}

# provider → 专属字段前缀（"通用字段 ↔ provider 专属字段"映射只在这里实现一次）
_LLM_PROVIDER_PREFIX = {"openai": "openai_llm", "qwen_vl": "qwen_vl"}

# provider → 该 provider 的代码默认值（通用默认值是按 openai 定的）
_LLM_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": {
        "model": "gpt-4o-mini",
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
    },
    "qwen_vl": {
        "model": "qwen3.6-flash",
        "api_key": "",
        "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    },
}

# 会被映射到 provider 专属字段的通用字段
_LLM_GENERIC_FIELDS = ("model", "api_key", "base_url")


class UpstreamConfigUnavailable(RuntimeError):
    """上游配置**不可用**：DB 读不到，且本进程没有可用的历史配置。

    这是**故意抛出的**，不是可以"再等等就好"的错误：
    继续用一个来路不明的配置去请求上游，只会把"配置没读到"伪装成
    "上游挂了/鉴权失败"，把排查方向带偏（这正是 2026-11-09 事故里最贵的一课）。

    处置方式（按优先级）：
      1. 管理后台「LLM 设置」/「TTS 设置」里把这组配置补齐；
      2. 或在服务器上跑 `python backend/deploy/seed_upstream_config.py --env-file <env>`，
         从 env 一次性播种进 DB；
      3. 若本进程刚重启且 DB 还没恢复，等 DB 恢复后重试即可（此时会有 last-good）。
    """

    kind = "config_unavailable"


# ── 规范化（纯函数，不碰 DB / env）───────────────────────────────────────
def _is_set(value: Any) -> bool:
    """DB 里"这个字段配了没有"。

    空串 / None 都算**没配** —— 与 admin 端点的合并语义一致
    （admin 用空串表示"用户没重新输入"，不传表示"不动"）。
    """
    return value is not None and value != ""


def resolve_llm(stored: dict | None) -> dict[str, Any]:
    """DB 里 `llm` 那一行 → 规范化配置（通用字段 + provider 专属字段）。

    纯函数：不读 DB、不读 env —— 这样 admin 端点（GET 回显）与运行期
    （factory/app 层）走的是**同一份解析实现**，不会漂移。

    合并语义（与既有 admin 端点一致）：
      · 代码默认值打底；
      · DB 行里非空的字段覆盖默认值；
      · **通用字段（model/api_key/base_url）优先于** provider 专属字段
        （管理后台存的就是通用字段），并按当前 provider 同步到专属字段
        —— 工厂按 provider 专属字段建 client，两边因此永远是同一份值。

    未知 provider ⇒ 专属字段一律清空 ⇒ 建 client 时报"不支持的 provider"，
    而不是悄悄用别的 provider 的凭据去打。
    """
    row = dict(stored or {})
    cfg: dict[str, Any] = dict(LLM_CODE_DEFAULTS)

    provider = str(row.get("provider") or cfg["provider"]).strip().lower()
    cfg["provider"] = provider

    # 调用预算 + OCR 模型：DB 里配了才覆盖（数值 0 也是有效值，用 _is_set 判空会误伤，
    # 所以预算类字段单独按"键存在且非 None"处理）。
    for field in ("timeout", "max_retries", "connect_timeout", "ocr_model"):
        value = row.get(field)
        if value is not None and value != "":
            cfg[field] = value

    prefix = _LLM_PROVIDER_PREFIX.get(provider)
    prov_defaults = _LLM_PROVIDER_DEFAULTS.get(provider, {})

    for field in _LLM_GENERIC_FIELDS:
        specific_key = f"{prefix}_{field}" if prefix else None
        # 优先级：DB 的通用字段 → DB 的 provider 专属字段 → 该 provider 的代码默认值
        value = row.get(field)
        if not _is_set(value) and specific_key:
            value = row.get(specific_key)
        if not _is_set(value):
            value = prov_defaults.get(field, LLM_CODE_DEFAULTS[field])
        cfg[field] = value
        if specific_key:
            cfg[specific_key] = value

    if prefix is None:
        # 未知 provider：专属字段清空 ⇒ 建 client 时明确报"不支持的 provider"
        for known_prefix in _LLM_PROVIDER_PREFIX.values():
            cfg[f"{known_prefix}_model"] = ""
            cfg[f"{known_prefix}_api_key"] = ""
            cfg[f"{known_prefix}_base_url"] = ""

    # DB 行里可能出现代码默认值没有的字段（如将来新增），原样带上
    for field, value in row.items():
        if field not in cfg:
            cfg[field] = value

    return cfg


def resolve_tts(stored: dict | None) -> dict[str, Any]:
    """DB 里 `tts` 那一行 → 规范化配置。

    TTS 的 DB 行**本来就是 provider 专属命名**（openai_api_key / indextts_model …），
    所以这里只需要"代码默认值打底 + DB 非空字段覆盖"。

    ⚠️ `null`（admin 显式清除后的值）算**没配** ⇒ 落回代码默认值，
    **不回落 env** —— 这正是收敛的目的：DB 说了算。
    """
    cfg: dict[str, Any] = dict(TTS_CODE_DEFAULTS)
    row = dict(stored or {})
    for field, value in row.items():
        if _is_set(value):
            cfg[field] = value
    cfg["provider"] = str(cfg.get("provider") or TTS_CODE_DEFAULTS["provider"]).strip().lower()
    return cfg


# ── 进程内缓存（last-good + 当前生效值）─────────────────────────────────
#
# 为什么缓存放进程内而不是每次都查 DB：
#   · 上游配置是**低频变更**的（管理后台手改），但**高频读取**（每次蒸馏）；
#   · DB 抖动的窗口里，我们希望继续用**上次确认过的**配置，而不是直接罢工；
#   · 缓存的另一个作用是让**同步**调用点（factory.get_llm_client 等）
#     也能拿到配置 —— DB 读取是 async 的，同步路径没法现场查库。
_last_good: dict[str, dict[str, Any]] = {}


def _remember(key: str, cfg: dict[str, Any]) -> None:
    _last_good[key] = dict(cfg)


def last_good(key: str) -> dict[str, Any] | None:
    """本进程最后一次成功读到的配置（没有则 None）。"""
    cfg = _last_good.get(key)
    return dict(cfg) if cfg is not None else None


def reset_for_tests() -> None:
    """清空进程内缓存（仅测试用）。"""
    _last_good.clear()


async def _load(key: str, resolver) -> tuple[dict[str, Any], str]:
    """读 DB → 规范化 → 记 last-good。

    返回 (配置, 来源)：`db` / `last_good` / `defaults`。
    读失败且没有 last-good ⇒ 抛 `UpstreamConfigUnavailable`。
    """
    try:
        stored = await get_config(key)
    except Exception as exc:  # noqa: BLE001 —— PG/Redis 都可能抛
        cached = _last_good.get(key)
        if cached is not None:
            log.warning(
                "upstream_config_db_read_failed_using_last_good",
                key=key,
                error=str(exc),
            )
            return dict(cached), "last_good"
        raise UpstreamConfigUnavailable(
            f"读不到上游配置（system_config 表 key={key!r}）：{exc}。"
            "本进程也没有可用的历史配置，拒绝用一个来路不明的端点去请求上游。"
            "请在管理后台补齐该配置，或执行 backend/deploy/seed_upstream_config.py 播种。"
        ) from exc

    cfg = resolver(stored)
    # 行不存在（stored is None）⇒ 用代码默认值，并**如实**记成 defaults 来源。
    # 这会覆盖 last-good：配置被人删掉还拿旧值继续跑，只会把问题掩盖掉。
    _remember(key, cfg)
    return dict(cfg), ("db" if stored is not None else "defaults")


async def load_llm() -> tuple[dict[str, Any], str]:
    """读 DB 的 llm 行 → (规范化配置, 来源)。"""
    return await _load(KEY_LLM, resolve_llm)


async def load_tts() -> tuple[dict[str, Any], str]:
    """读 DB 的 tts 行 → (规范化配置, 来源)。"""
    return await _load(KEY_TTS, resolve_tts)


def current_llm() -> dict[str, Any]:
    """同步取当前生效的 LLM 配置（进程内缓存）。

    同步路径（factory.get_llm_client 等）没法现场查库，所以只读缓存。
    缓存为空 ⇒ 抛 `UpstreamConfigUnavailable` —— **不回落 env**。
    """
    cfg = _last_good.get(KEY_LLM)
    if cfg is None:
        raise UpstreamConfigUnavailable(
            "上游 LLM 配置尚未加载（进程内没有可用的 DB 配置）。"
            "请确认 system_config 表里有 llm 行，并调用过 reload()。"
        )
    return dict(cfg)


def current_tts() -> dict[str, Any]:
    """同步取当前生效的 TTS 配置（进程内缓存）。没有 ⇒ 抛明确异常。"""
    cfg = _last_good.get(KEY_TTS)
    if cfg is None:
        raise UpstreamConfigUnavailable(
            "上游 TTS 配置尚未加载（进程内没有可用的 DB 配置）。"
            "请确认 system_config 表里有 tts 行，并调用过 reload()。"
        )
    return dict(cfg)


# ── 启动自检（诊断）─────────────────────────────────────────────────────
def _missing_required_llm(cfg: dict[str, Any]) -> list[str]:
    provider = str(cfg.get("provider") or "").lower()
    missing: list[str] = []
    if not provider:
        missing.append("provider")
    if not _is_set(cfg.get("base_url")):
        missing.append("base_url")
    if provider == "openai" and not _is_set(cfg.get("api_key")):
        missing.append("api_key")
    elif provider.startswith("qwen") and not _is_set(cfg.get("qwen_vl_api_key")):
        missing.append("qwen_vl_api_key")
    return missing


def _missing_required_tts(cfg: dict[str, Any]) -> list[str]:
    provider = str(cfg.get("provider") or "").lower()
    missing: list[str] = []
    if not provider:
        missing.append("provider")
    if provider == "openai":
        if not _is_set(cfg.get("openai_base_url")):
            missing.append("openai_base_url")
        if not _is_set(cfg.get("openai_api_key")):
            missing.append("openai_api_key")
    elif provider == "doubao":
        if not _is_set(cfg.get("doubao_api_key")) and not _is_set(cfg.get("doubao_token")):
            missing.append("doubao_api_key")
    return missing


async def diagnose() -> dict[str, Any]:
    """启动自检：报告两组上游配置的状态（**只读**，不改任何东西）。

    目的：把"配置没读到 / 没配全"从**运行期的神秘失败**提前到**启动时可见**。
    生产 DB 明明配好了却没生效、或压根没配，这两种情况都在这里点名。
    """
    report: dict[str, Any] = {}
    for name, key, loader, missing_fn in (
        ("llm", KEY_LLM, load_llm, _missing_required_llm),
        ("tts", KEY_TTS, load_tts, _missing_required_tts),
    ):
        try:
            cfg, source = await loader()
        except UpstreamConfigUnavailable as exc:
            report[name] = {"ok": False, "source": "unavailable", "error": str(exc)}
            log.error("upstream_config_diagnose_unavailable", key=name, error=str(exc))
            continue
        missing = missing_fn(cfg)
        report[name] = {
            "ok": not missing,
            "source": source,
            "provider": cfg.get("provider"),
            "model": cfg.get("model") or cfg.get(f"{cfg.get('provider')}_model"),
            "base_url": cfg.get("base_url") or cfg.get(f"{cfg.get('provider')}_base_url"),
            "api_key_set": bool(cfg.get("api_key") or cfg.get("openai_api_key")),
            "missing": missing,
        }
        if missing:
            log.warning(
                "upstream_config_incomplete",
                key=name,
                provider=cfg.get("provider"),
                missing=missing,
            )
        else:
            log.info(
                "upstream_config_ok",
                key=name,
                provider=cfg.get("provider"),
                base_url=report[name]["base_url"],
                source=source,
            )
    return report


__all__ = [
    "LLM_CODE_DEFAULTS",
    "TTS_CODE_DEFAULTS",
    "UpstreamConfigUnavailable",
    "current_llm",
    "current_tts",
    "diagnose",
    "last_good",
    "load_llm",
    "load_tts",
    "reset_for_tests",
    "resolve_llm",
    "resolve_tts",
]
