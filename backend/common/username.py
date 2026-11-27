"""唯一用户名（username）的生成规则 —— **单一真相源**。

为什么要有 username 这个字段
----------------------------
`users.nickname` 是**显示名**，不是标识：微信登录建号时它是硬编码的常量
「听友」（user-service/main.py:480），所以**所有内测账号的昵称都一样**。
而 `users` 表既没有唯一约束也没有 nickname 索引 ⇒ 管理后台按昵称搜不出
「是哪一个」，两张同名「听友」在运营眼里是同一个人。

⇒ 把三件事拆开，各归其位（别再混着用）：

  | 概念         | 存哪              | 唯一性 | 给人看吗 |
  |--------------|-------------------|--------|----------|
  | 显示名       | `nickname`        | 可重复 | 是        |
  | **唯一用户名**| `username`（本模块）| **全局唯一** | 是（后台/本人） |
  | 设备指纹     | 客户端上报，不落库 | 设备内恒定 | 否（只做输入） |

username 在「微信登录尚未上线」期间是**暂用**的：拿**手机系统自带、零额外权限**
能拿到的**设备特征**生成 ⇒ 同一台设备恒定同一个 username，换设备就是另一个。

取值规则（v1 = 设备派生）
-------------------------
    d-<机型10位>_<设备指纹8位>
    例：d-XIAOMI14PR_3F9A2C71   d-HUAWEIMATE_B21E8D40

* **机型**：客户端上报 `Build.MANUFACTURER + Build.MODEL`
  （⇒ "具备特征性"：后台一眼看出是谁的哪台机器）。只保留字母数字、转大写、
  截断 10 位 —— 机型名带空格与中文（"Redmi K80 5G"），必须规范化。
* **设备指纹**：客户端上报 `DeviceId.androidIdSuffix()`
  （= SHA-256(ANDROID_ID) 前缀，**同一 App 签名 + 同一手机用户下跨重装恒定**，
  见 stashbox-android `util/DeviceId.kt`）⇒ 保证唯一。
* **`d-` 前缀**：标明来源是**设备派生**（暂用形态）。
  与仓内 `and-` / `rnd-` 的既有约定同源 —— "让后台一眼看出这个 id 的来源"。
  微信登录上线后改用 `w-` 前缀，形态切换一眼可辨。
* **取不到机型/指纹时**：回落到 `t-<用户id>`（`t` = 兜底），保证非空且唯一。

形态前缀一览（后台一眼能分辨一个用户名是哪种形态）
--------------------------------------------------
| 前缀 | 含义                        | 何时产生                       |
|------|-----------------------------|--------------------------------|
| `d-` | 设备派生（暂用形态）        | 登录时客户端上报了设备特征     |
| `t-` | 兜底形态（无设备信息）      | `t-<用户id>`；ORM 层随机兜底 `t-<随机hex>` |
| `w-` | 微信派生                    | 微信登录上线后（v1 未使用）    |

唯一性
------
* 应用层生成 + **DB 唯一索引兜底**（`uq_users_username_lower`，
  建在 `lower(username)` 上 ⇒ 大小写不敏感）——
  唯一性最终由 DB 保证，不靠应用层自觉。
* 冲突时（同一台设备因指纹截断/清零而落到同名，或同设备建了两个账号）
  依次尝试 `-2` / `-3` …；仍冲突则回落到 `t-<用户id>`。

⚠️ **可改性**：username 在微信登录上线时会被**整体迁移**（设备派生 → 微信派生），
所以它必须是**可改**的。v1 不做改名接口（内测期没人要改），但**列上不写
不可变约束**，迁移到微信形态时直接 UPDATE 即可。
"""

from __future__ import annotations

import re

#: username 总长上限（DB 列宽 VARCHAR(32)）。
USERNAME_MAX_LEN = 32

#: 机型部分截断长度（"HUAWEIMAT" 比 "HUAWEIMAT" + 完整机型更紧凑）。
MODEL_PART_LEN = 10

#: 设备指纹部分长度（8 位 hex = 32 bit，同一机型下的碰撞概率可忽略）。
FINGERPRINT_LEN = 8

#: 兜底形态前缀（取不到设备信息时用用户 id）。
FALLBACK_PREFIX = "t"

#: 设备派生形态前缀。
DEVICE_PREFIX = "d"

#: 微信派生形态前缀（微信登录上线后启用，v1 未使用）。
WECHAT_PREFIX = "w"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

#: 客户端上报设备指纹时可能带的**来源前缀**（与安卓 `util/DeviceId.kt` 同源）。
#: `and-` = ANDROID_ID 哈希（跨重装稳定），`rnd-` = 兜底随机（仅本机安装内稳定）。
#:
#: 为什么要在规范化时**去掉**它：前缀是"来源说明"，不是哈希本体。
#: 本体长度随前缀不同而不同（12 位 vs 16 位）⇒ 不去掉就无法用同一个取位规则。
_SOURCE_PREFIXES = ("and", "rnd")


def normalize_model(raw: str | None) -> str:
    """机型规范化：去非字母数字 → 转大写 → 截断到 10 位。

    >>> normalize_model("Xiaomi 14 Pro")
    'XIAOMI14PR'
    >>> normalize_model("Redmi K80 5G")
    'REDMIK805G'
    >>> normalize_model("HUAWEI Mate 60 Pro")
    'HUAWEIMATE'
    >>> normalize_model(None)
    ''
    """
    if not raw:
        return ""
    cleaned = _NON_ALNUM.sub("", raw).upper()
    return cleaned[:MODEL_PART_LEN]


def normalize_fingerprint(raw: str | None) -> str:
    """设备指纹规范化：去来源前缀 → 只留字母数字 → 转大写 → 取**开头** 8 位。

    ## 为什么先去前缀再取位（这是本函数最要紧的一步）

    客户端上报的设备指纹有三种形态，**本体长度不一样**：

    | 上报形态              | 本体长度 | 出处                              |
    |-----------------------|---------|-----------------------------------|
    | `and-<12位hex>`       | 12      | `DeviceId.analyticsDeviceId()`   |
    | `rnd-<16位hex>`       | 16      | 同上（取不到可靠 ANDROID_ID 时）  |
    | 裸 `<12位hex>`         | 12      | `DeviceId.androidIdSuffix()`     |

    ⇒ **去掉 `and-` / `rnd-` 之后，"哈希本体"的位数才对齐**，
    取开头 8 位 ⇒ **同一台设备不论上报哪种形态都得到同一个指纹**。

    反过来，如果按"整串取末 8 位"来做：
    `and-3f9a2c71deadbeef` → `DEADBEEF`（含的是前缀之后的尾段），
    而裸 `3f9a2c71deadbeef` → `DEADBEEF` —— 看似一致，实则**取位随本体长度漂移**
    （12 位本体取末 8 与 16 位本体取末 8 不是同一段）⇒ 同一台设备可能因为
    上报形态不同而拿到两个用户名 ⇒ "同一台设备恒定同名"这条契约就破了。

    ## 为什么取开头而不是末位

    取**开头**还有个好处：哈希本体是 `SHA-256(...)` 的**前若干位**前缀
    （安卓端 `androidIdSuffix` = `sha256Hex(id).take(12)`），
    开头就是哈希最"本体"的那段，末位更接近被截断的尾巴。

    >>> normalize_fingerprint("and-3f9a2c71deadbeef")
    '3F9A2C71'
    >>> normalize_fingerprint("rnd-0123456789abcdef")
    '01234567'
    >>> normalize_fingerprint("3f9a2c71dead")
    '3F9A2C71'
    """
    if not raw:
        return ""
    value = raw.strip()
    # 去来源前缀（只在真带分隔符时去；裸 hex 不含 'n' ⇒ 不会误伤）
    lowered = value.lower()
    for prefix in _SOURCE_PREFIXES:
        if lowered.startswith(f"{prefix}-"):
            value = value[len(prefix) + 1 :]
            break
    cleaned = _NON_ALNUM.sub("", value).upper()
    return cleaned[:FINGERPRINT_LEN]


def build_username(
    device_id: str | None = None,
    device_model: str | None = None,
    user_id: int | None = None,
) -> str:
    """生成唯一用户名的**候选**（不保证全局唯一 —— 那由唯一索引 + 重试保证）。

    Args:
        device_id: 客户端上报的设备指纹（`and-…` / `rnd-…` / 裸 hash 均可）。
        device_model: 客户端上报的机型（`Build.MANUFACTURER + Build.MODEL`）。
        user_id: 用户 id，用于兜底形态。

    Returns:
        `d-<机型>_<指纹>`；信息不足时 `t-<user_id>`。
    """
    model = normalize_model(device_model)
    fingerprint = normalize_fingerprint(device_id)
    if model or fingerprint:
        # 分隔符约定：`d-` 用**连字符**（与 `and-`/`rnd-` 同源，形如来源标记），
        # 机型与指纹之间用**下划线**（同一用户名里的两个字段分隔）。
        head = f"{DEVICE_PREFIX}-"
        if model and fingerprint:
            return f"{head}{model}_{fingerprint}"[:USERNAME_MAX_LEN]
        if model:
            return f"{head}{model}"[:USERNAME_MAX_LEN]
        return f"{head}{fingerprint}"[:USERNAME_MAX_LEN]
    if user_id is not None:
        return f"{FALLBACK_PREFIX}-{user_id}"
    return f"{FALLBACK_PREFIX}-0"


def username_candidates(
    device_id: str | None = None,
    device_model: str | None = None,
    user_id: int | None = None,
    max_attempts: int = 5,
) -> list[str]:
    """依次给出候选名，供"插入时撞唯一索引 ⇒ 换下一个"的重试使用。

    顺序：设备形态 → 设备形态-2 → -3 … → 兜底 `t-<id>`。
    """
    base = build_username(device_id, device_model, user_id)
    out = [base]
    for n in range(2, max_attempts + 1):
        suffix = f"-{n}"
        candidate = base[: USERNAME_MAX_LEN - len(suffix)] + suffix
        out.append(candidate)
    if user_id is not None:
        fallback = f"{FALLBACK_PREFIX}-{user_id}"
        if fallback not in out:
            out.append(fallback)
    return out


def is_valid_username(value: str | None) -> bool:
    """用户名合法性（admin 改名 / 导入时的校验口径）。

    * 长度 1..32
    * 只允许字母数字、`-`、`_`
    * 不允许以分隔符开头/结尾
    """
    if not value:
        return False
    if len(value) > USERNAME_MAX_LEN:
        return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*[A-Za-z0-9]", value):
        return False
    return True


async def assign_username(
    db,
    user,
    device_id: str | None = None,
    device_model: str | None = None,
    max_attempts: int = 5,
) -> str:
    """给 user 分配一个**未被占用**的 username 并写回（唯一索引冲突时自动换候选）。

    为什么用 SAVEPOINT（`begin_nested`）而不是直接重试
    --------------------------------------------------
    唯一索引冲突会让**整个事务**进入 aborted 状态，直接重试必然二次失败。
    `begin_nested()` 发的是 SAVEPOINT：冲突只回滚到保存点，
    外层事务（用户的 insert）仍然活着 ⇒ 可以换下一个候选继续试。

    ⚠️ 调用方必须在调用本函数**之前**已经 flush 过 user（要拿到 user.id 做兜底候选）。
    """
    from sqlalchemy.exc import IntegrityError

    candidates = username_candidates(device_id, device_model, user.id, max_attempts)
    last_error: Exception | None = None
    for cand in candidates:
        try:
            async with db.begin_nested():
                user.username = cand
                await db.flush()
            return cand
        except IntegrityError as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"无法为 user {getattr(user, 'id', None)} 分配唯一 username"
        f"（试了 {len(candidates)} 个候选）"
    ) from last_error
