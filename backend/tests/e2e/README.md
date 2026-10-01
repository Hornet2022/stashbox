# 真机端到端用例库

以**真机 App 为唯一入口**，覆盖一个真实客户从剪藏到收听评分的完整动线。

## 为什么是这个形态

- 不引入 Appium / UIAutomator2。目标机器上只有 `adb`，引入等于先装一整套环境。
- 断言重心放在**跨端一致性**：点了按钮 → 后端状态对不对。
  只断言「按钮在不在 / 点了没报错」信息量太低 —— 本项目反复出现的正是
  "界面显示已保存、后端其实没落库"这类静默失灵。
- 每个用例都能在失败时**自证**：截图 + UI dump 自动存到 `/tmp/stashbox-e2e/`，
  断言消息里直接带当时的完整 UI 树，不用重跑就能看。

## 跑法

```bash
cd /Users/hornet/work/stashbox

# 全部用例
STASHBOX_ALLOW_DEV_JWT=1 PYTHONPATH="/Users/hornet/work:$(pwd)/backend" \
  backend/.venv/bin/python -m pytest backend/tests/e2e -v

# 单组
... -m pytest backend/tests/e2e/test_e2e_02_detail_evaluation.py -v

# CI 无设备时只跑不需要设备的契约用例
STASHBOX_E2E_SKIP_DEVICE=1 ... -m pytest backend/tests/e2e -v
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `STASHBOX_E2E_SERIAL` | `f1a9e47d` | 设备序列号 |
| `STASHBOX_GATEWAY` | `http://127.0.0.1:8100` | 网关地址 |
| `STASHBOX_E2E_EVIDENCE` | `/tmp/stashbox-e2e` | 截图 / UI dump 落地目录 |
| `STASHBOX_E2E_SKIP_DEVICE` | — | 设 `1` 跳过所有需要设备的用例 |
| `STASHBOX_TEST_DSN` | `postgresql://stashbox:stashbox_dev@localhost:5432/stashbox` | 直连 DB |

## 用例清单

| 文件 | 覆盖 | 是否需要设备 |
|---|---|---|
| `test_e2e_01_home.py` | 冷启动、首页导航、**副标题不出现机器码**、无崩溃 | 是 |
| `test_e2e_02_detail_evaluation.py` | 详情页就绪态、**已评分冷启动后仍在**、**「修改评分」弹窗能开**、进度落库、App 确实在上报 | 部分 |
| `test_e2e_03_quota.py` | 配额字段名契约、接口与库一致、**配额耗尽返 403+3001**、`/distill/start` 越权与不破坏旧产物 | 否 |
| `test_e2e_04_capture_favorite.py` | **剪藏只扣 1 次费**、**重复剪藏幂等**、收藏/取消、稍后听/取消、`snooze_until` 落库、删除级联清理、**非属主删不了别人的文章**、配额耗尽 D9 被拒 | 否 |
| `test_e2e_05_tags_settings.py` | 标签列表带 `subscribed`、订阅/取消订阅、重复订阅幂等、通知列表可访问、source 不是机器码 | 否 |
| `test_e2e_06_device_navigation.py` | 底部四个 Tab 齐全、收藏页能打开、订阅页能加载、稍后听页能打开、快速切页无 ANR | 是 |

## 这套用例抓出来的真实 bug（都已修）

### 1. 剪藏一篇文章扣 2 次配额（CP-AI-CHARGE-DUP）

**现象**：免费档 5 篇/月，实际只能剪藏 2.5 篇，且**没有任何报错**。

**根因是跨服务的双重扣费**：

```
content-service  d9_add_article
  1. quota_service.consume()                  ← 扣 1
  2. cache_service.mark_article_quota(art.id) ← 在 Redis 打「已扣配额」标
  3. trigger_distill() → ai-service
       ai-service  distill_article
         existed_da = select ... where article_id = ...   ← 此刻还没有行
         already_charged = (existed_da is not None)      ← False
         quota_service.consume()                         ← 又扣 1
```

content-service 打的 Redis 标，ai-service 从来没读过 ——
`has_article_quota()` 写了却**全项目零调用**，漏的就在这一步。

值得注意的是 `distill_article` 的 docstring 写的是
「CP1.7.4 幂等修复：复用判定从『已扣过配额』改为『已有完整蒸馏产物』，
不再把已在 content-service 抓取阶段扣过的配额误算入蒸馏阶段」——
**注释描述的方向和代码实际行为正好相反**，代码干的正是"把抓取阶段扣的那次又算了一遍"。

**修复**：`already_charged = existed_da is not None or marked`。`/distill/start` 同步修。

**回归**：`test_d9_clip_charges_exactly_once_across_services`。

### 2. 重复剪藏同一链接 → 重复建文 + 重复计费（CP-DUPLICATE-CLIP）

**现象**：同一 URL POST 两次拿到两个不同 `article_id`，配额扣两次，库里两行。

**根因**：`articles` 表对 `(user_id, url)` **没有任何唯一约束**，
`_create_article` 无条件 `db.add(art)`。且 D9 / `submit_article` 都是
「先 `consume` 扣费 → 再建文章」，即使复用了已有文章，钱也已经扣掉了。

**修复**：
1. `_create_article` 增加查重（`dedup=True`，同用户 + 同 URL + 未删 → 复用），
   `_find_existing_clip` 助手供调用方在建行前判断。
2. `d9_add_article` / `submit_article` 把查重**挪到扣费之前**，
   命中重复时直接返回已有文章，**配额不动**。

**口径**：只查同一用户。不同用户剪藏同一链接各自拥有一份（属主隔离 + 配额按用户计），
这是产品语义不是 bug；已软删的允许重新剪藏。

**回归**：`test_d9_callback_idempotent_same_url`（同时断言库里有且仅有 1 行）。

## 踩过的坑（都写进注释了，别再踩）

1. **`adb input tap` 对 Compose 按钮偶尔不触发** → 用 `input swipe x y x y 100`（同点按压 100ms）。
2. **坐标不能缓存**：底部播放条出现/消失会改变布局，必须每次 `uiautomator dump` 重新取。
3. **`dumpsys ... | grep x` 在设备端过滤不可靠**：grep 没匹配时返回 1，`adb shell` 把退出码带回来，
   只读查询会误报失败。改成拉原始输出在 Python 侧过滤（见 `shell_readonly`）。
4. **表名别猜**：`distillation_evaluations`（不是 `listening_evaluations`），
   且它**没有 article_id 列**，靠 `task_id` 关联 `distilled_articles`。
5. **收听进度是 upsert**：`UNIQUE(user_id, article_id)`，行数永不变；
   值相同时后端还会跳过写入。断言要落在 `position_sec` 变化上，
   或用 logcat 验 App 行为，别指望 `updated_at` 每次都动。
6. **SQLAlchemy 的连接池不能跨 event loop 复用**：每条用例新建 event loop 去
   `AsyncSessionLocal()` 读写，第二次就炸 `got Future ... attached to a different loop`
   / `Event loop is closed`。**改数据一律用 asyncpg 直连**（见 `test_e2e_04` 的
   `_write_quota`）。尤其注意 `finally` 里的恢复 —— 恢复不回来会让后面
   所有依赖该状态的用例连锁 skip，看起来像"环境有问题"，实际是测试自己把环境改坏了。
7. **表名和列名别猜**：`feedback`（有 `type` 列）≠ `feedback_v2`（有 `category` 列），
   收藏写的是前者；`tags` 接口返回的 `id` 是 **slug**（`"news"`）不是数据库主键（`1`），
   而 `tag_subscriptions.tag_id` 是 integer —— 查库得 `join tags on t.slug = $1`。
8. **测试数据必须自带清理**：04 组挂在 user 1（真机在用账号）名下造文章，
   不清理就会直接出现在真机 App 列表里。本轮跑完留了 32 篇 `e2e_capture_*`，
   排在列表最前，把 02 组「详情页显示已就绪」顶掉了 —— 那个失败看着像产品 bug，
   实际是自己污染被测环境。现在 04 每条用例的 `finally` 都调 `_purge_by_url`。
   （删除顺序有讲究：先子表后父表，直接 `delete from articles` 会撞
   `distilled_articles_article_id_fkey`。）
9. **选测试数据要按属主过滤**：`App` 详情端点按属主校验，库里若躺着别的用户的
   done 文章（dev 造的、其他 e2e 留下的），不加 `a.user_id = 1` 就会选中一篇
   App 打不开的文章，表现为「详情页没出现」或 403。本轮就选中过 user 13137 的文章。
10. **`am start` deep link 不要带 `-n`**：带了 `-n` 就是显式指定 component，
   绕过 intent-filter 匹配，deep link 静默失效（App 停在首页，判据永远不满足）。
    正确写法 `am start -a android.intent.action.VIEW -d 'stashbox://detail/<id>'`。
11. **`uiautomator dump` 本身不稳**：App 播放中 / 转场时拿不到窗口快照，
    命令非零退出且 **stderr 为空**。`driver.dump()` 已加 3 次重试 ——
    遇到就重试，别当成产品 bug。
12. **`healthz` 200 不等于「跑的是最新代码」**：本轮排查「谁把真机那篇文章改成
    failed」时，先怀疑后台进程、查了 arq 队列、翻了两个服务日志，全是空。
    实际是我自己重启 ai-service 时 PYTHONPATH 没带对，服务直接启动失败
    （`ModuleNotFoundError: No module named 'stashbox'`），监听 8103 的一直是
    **旧进程** —— 于是本轮的修复根本没生效，我却以为已经生效。
    教训：改完服务要确认启动日志无 import 错误；验证修复前先手动复现一遍
    旧行为，确认「修之前确实是坏的」，否则你不知道自己在验什么。
13. **调试别停在「猜测」**：那次的正确顺序是
    `查 updated_at 时间点 → 对齐自己刚跑过什么 → 查监听进程的实际启动时间`。
    第三步就定位到了：监听进程的启动时间比我改代码还早。
14. **logcat 不是 UTF-8**：`logcat -d` 输出里混着非 UTF-8 字节（设备端 C 层日志、
    崩溃转储），subprocess 默认按 locale 解码会抛 `UnicodeDecodeError`，
    于是「读日志」变成「用例失败」。driver 统一 `errors="replace"`。
    实测一次 logcat 48 万字节处就有个 `0xc0`。

## 2026-09-30 事故：蒸馏全线失败（6 层原因叠加）

现象：真机剪藏的文章永远停在 `distilling` → `failed`，日志里 6 次失败全是
`agent distill failed at tts ... IndexTTS 合成失败（已尝试 4 次 / 2 个实例）`。
**10:47 就已经在失败了，比那次剪藏更早** —— 顺着「第一次失败是什么时候」查下去，
挖出四层独立的原因，任何一层单独存在都足以让蒸馏必挂。

15. **`.env` 是相对 CWD 解析的，而各服务 CWD 不统一**。`common/config.py` 用
    `SettingsConfigDict(env_file=".env")`，相对进程 CWD 找文件。四个服务的
    LaunchAgent 里，api-gateway / content-service / user-service 的 CWD 是
    `backend/`，**ai-service 和 ai-worker 的 CWD 是 `backend/ai-service/`** ——
    于是真正干蒸馏的两个组件**从来没读到过 `.env`**。表现极具迷惑性：LLM 改写
    正常（配置存在 `system_config` 表里，DB > env），TTS 却报「需要参考音频」
    （那只在 `.env` 里）。修法见 `common/config.py` 的 CP-ENV-CWD 注释。
    ⚠️ 只改 `env_file` 路径**不够**：pydantic-settings 只把 `.env` 灌进 Settings
    对象，不写 `os.environ`，而 `app/services/llm/__init__.py` 是直接
    `os.getenv()` 读的 —— 必须显式 `load_dotenv` 绝对路径。
16. **launchd `KeepAlive` + 服务端 daemonize = 实例增生**。oMLX 的 LaunchAgent
    配了 `KeepAlive=true`，但 `omlx-cli serve` 会把真正的 `omlx-server`
    daemonize 掉，launchd 手里追踪的 PID 随即退出 → launchd 认为「服务挂了」
    → 再拉一个。实测同时跑过 **4 个** oMLX 实例，每个都预加载 1.4~2.3GB 的 TTS
    模型 → 内存打爆 → 空转（CPU 高、RSS 不涨、合成端点零响应）。
    这就是文件头 `indextts.py:41` 记的那个现象的一半成因。已把 plist 改成
    `KeepAlive=false` + `RunAtLoad=true`。
17. **oMLX 内存守卫会反复卸载模型**。`~/.omlx/settings.json` 里
    `memory_guard_tier=balanced` / `soft_threshold=0.85`；16GB 机器上软阈值
    ≈13.6GB，而本机 swap 常年占 7GB+（陈旧页），守卫误判成内存吃紧 →
    把已加载的 TTS 模型卸掉 → 下个请求冷加载 2.26GB → 撞 90s 超时。
    已给 serve 加 `--memory-guard off`。
18. **分段参数与实测速度不匹配**。实测 Qwen3-TTS 约 **0.7s/字**
    （18 字 → 11.6s；378 字 → **267.9s**），而 `.env` 是
    `CHUNK_CHARS=400` + `CHUNK_TIMEOUT=90` —— 每段都要跑 ~280s 却被 90s 掐断。
    现改为 `CHUNK_CHARS=200` + `CHUNK_TIMEOUT=240`。**这是 TTS 慢的根因，
    不是连接问题** —— 之前一直误判成「服务连不上」而反复加 failover。
    完整修复后单篇 2193 字实测 16 段、`1419.87s` 跑完并产出 570s 音频。
19. **Postgres 数据目录被删且不可恢复**：09-28 11:38 数据目录消失，launchd
    重试 17284 次全失败（`could not access directory ... Run initdb`）。
    全盘搜 `PG_VERSION`（深度 8，含外置卷）**什么都没有**，无 Time Machine、
    无本地快照、无容器卷 —— 开发数据全丢，只能 `initdb` + `alembic upgrade head`
    重建。**教训：数据目录消失时先 `initdb` 是错的**，先确认全盘搜过再说。
20. **`alembic 0032` 不幂等 → 空库一张表都建不出来**。它假设
    `push_notifications_tag_slug_fkey` 不存在才补建，但**空库上 0008 的
    `create_table` 会正常发出这条 FK**，于是报 DuplicateObjectError。alembic
    用事务型 DDL，一个迁移失败会**回滚整条链** —— 表现为「initdb 之后
    `upgrade head` 建表失败」，但表数是 0 而不是 19。
    已改成先查存在性再动手（见该迁移的 `_fk_exists` / `_index_exists`）。
21. **`tools.py` 的 `log` 是 stdlib logging，不是 structlog**。同一份代码里
    `distill_task.py` 用 structlog（`log.info("event", key=value)` 合法）、
    `agent/tools.py` 用 `logging.getLogger`（同样写法会抛
    `Logger._log() got an unexpected keyword argument 'article_id'`）。
    症状极怪：任务在 **tts 阶段**失败，但报错跟 TTS 毫无关系 —— 是我加的一行
    日志打错了格式。**跨文件加日志前先确认那个文件的 log 是什么类型**。


## 2026-09-30 自由拓展自测：端到端没覆盖到的 6 个 BUG

上一节的事故修完后，功能是通的，但「通」不等于「对」。这一轮不看覆盖率的洞，
专挑**端到端用例结构上照不到**的地方：并发、权限、降级、缓存、界面诚实性。
6 个 BUG 全部已修，每个都配了回归测试（见下表）。

| # | BUG | 为什么 e2e 照不到 | 回归测试 |
|---|---|---|---|
| 5 | 并发首次写偏好 → 500 | e2e 一次只点一下，撞不上竞态 | `tests/content/test_tts_voice_preference.py` |
| 6 | 蒸馏溯源 `tts_voice_id` 只写不读 | 「少了个字段」不会让任何请求失败 | `tests/content/test_tts_voice_trace.py` |
| 7 | 蒸馏完成后文章详情缓存不失效 | 断言都打在 `/status`（不走缓存）上 | `tests/ai/test_distill_cache_invalidation.py` |
| 8 | provider 不支持覆盖时溯源说谎 | 本机只有 indextts 这一个 provider | `tests/ai/test_tts_voice_trace_honesty.py` |
| 9 | 设置页加载失败仍显示笃定值 | 断网/401 不在用例路径里 | 真机手动验（停网关） |
| 10 | 「最近剪藏」卡片不可点 | 用例从列表页进详情，绕开了剪藏页 | 真机手动验（uiautomator clickable 节点） |

### 三个值得单独记的教训

**1. 竞态测试要盯「实现方式」，不要盯「跑 N 次会不会炸」。**
   BUG#5 最初写的是「并发 6 次写偏好，断言没有 5xx」。这测试在 pytest 的
   in-process ASGI 下**一次都复现不出来**（请求被天然串行化），旧实现照样全绿 ——
   是个只会给人安全感的空转测试。真正测住的是把断言换成
   「发往 `user_tts_preferences` 的语句必须只有 1 条且含 ON CONFLICT」：
   只要有人改回读-改-写，它必红，与调度顺序无关。
   临时回退验证过：干净回退后**恰好**是结构那条和空操作那条红，时序那条依旧绿。

**2. 写缓存的测试不能放在 Redis 被 mock 掉的目录。**
   `tests/ai/conftest.py` 有个 autouse fixture 把 `redis.asyncio.Redis` 整个换成
   no-op 桩（为测退款锁）。在那里写「先塞缓存再断言缓存没了」，
   `get_article` 恒返回 None，断言会因为桩而**假绿**。改成 spy 住
   `cache_service.invalidate_article` 记录调用，不依赖 Redis。

**3. 「少了个字段」是 e2e 照不到的经典盲区。**
   BUG#6：蒸馏链路老实把「这段音频是谁念的」写进库，但从头到尾**没有任何一处读它**。
   三端都不显示，`GET /articles/{id}` 照样 200、字段一个不少地少一个。
   而它唯一的展示位是「换音色→重生成」的验收凭据 —— 闭环看着接上了，其实断在最后一米。


## 与既有测试的分工

`tests/ai` `tests/gateway` `tests/admin` 是**接口/单元**测试，直接连共享库。
本目录是**真机端到端**，跑的是完整链路，断言跨越 UI / 接口 / DB 三层。

⚠ 已知问题（尚未修）：`tests/ai` `tests/admin` `tests/content` 三批仍直连共享开发库，
测试之间会互相删数据，已造成间歇性失败
（`test_distill_task_content.py::test_distill_task_uses_real_raw_content_from_db`
单独跑通过、批量跑偶发 error；`test_stats_enhanced.py` 的失败数在 5~8 间波动）。
ai-service 已有自建 fixture 的先例（commit `938a55b`），其余两批尚未跟进。
