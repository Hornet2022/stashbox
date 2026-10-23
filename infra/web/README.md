# 听匣 · 公开落地页

`index.html` 是品牌主站（`https://stashbox.cn/` 与 `https://www.stashbox.cn/`）。
**自包含静态页**：无构建、无依赖、无第三方资源 —— 一个文件就是全部。

## 它从哪来

由 `~/.prototypes/tingxia-landing/index.html`（2026-10-06 的「首页方向探索」）
生产化而来。原型 README 写着「状态：未决策、这只是探索」，这里做的是把它
变成一个**真的能上线的页面**。

### 生产化时改了什么

| 改动 | 为什么 |
|---|---|
| **默认方向 = 纸面**，且把它的 markup **静态预渲染**进 `#stage` | 原型是纯 JS 渲染的 → ①无 JS 看不到内容 ②**微信/Twitter 的分享爬虫不跑 JS，卡片会是空的**（内测主分享渠道就是微信，这是硬伤）③首屏不等 JS |
| 方向切换器**默认隐藏**（`?picker=1` 还能打开，`?v=1\|2\|3` 直接切） | 原型是「三个方向并排挑」的探索工具，对外是「没做完」的观感 |
| CTA「加入内测名单」→「**下载内测版**」 | 现在真的有可安装的 release APK 了；原型 README 里「CTA 还没定」这个阻塞已解除 |
| 方向 2 的样例音频换成**真实成品** | 原型里「《注意力经济下的深度阅读》· 样例」是占位，没有音频源 |
| 加 `og:` / `description` meta | 分享卡片的标题与描述 |
| **不再往地址栏写 `?v=1`** | 原型的 `setActive()` 会 `history.replaceState` 把当前方向写进 URL → 生产页每次打开都变成 `/?v=1`，**分享出去的链接带着这个噪音**。改成只在探索模式（`?picker=1`）下才写 |
| `<a class="btn">` 补 `text-decoration: none` | CTA 从 button 换成真链接 |

### 为什么默认选「纸面」

三个方向都留着（`?v=1` 纸面 / `?v=2` 声纹 / `?v=3` 通勤）。选纸面作默认的理由：

1. 它的 markup **完全静态**（方向 2 的波形是 JS 生成的）→ 能预渲染 → 分享卡有内容；
2. 它带「**它不做什么**」这一栏（不做信息流、不推算法推荐、不追热点）——
   对一个要区别于「又一个播客 App」的产品，这是最值钱的一句；
3. 方向 3 的时间线文案（个性化、预下载）我逐条核对过**都是真的**
   （`is_personalized` 14/14 为真、App 里有 `CommutePrefetcher`），
   但它「最复杂，叙事全靠文案撑着」；
4. 方向 2 的风险（原型 README 原话）：「音频隐喻容易被读成又一个播客 App」。

**这是设计决策，不是技术决策** —— 换默认方向只需改预渲染的那一段。

## 生产化时踩到的两个坑（都是「看起来没问题但整页是坏的」）

### ① `.enter` 的 `opacity: 0` 让内容**依赖动画才能看见**

原型写法：

```css
.enter { opacity: 0; animation: enter 250ms ... forwards; }
```

`opacity: 0` 是**基础样式**，只有动画跑完（且 `forwards` 把 1 定住）才看得见。
浏览器在**后台标签页**会暂停 CSS 动画 → 动画停在 0% → `forwards` 把
`opacity: 0` 定住 → **整页白**。

改法：**基础样式就是可见的**，动画只是「淡入」这个增强：

```css
.enter { opacity: 1; transform: none; animation: enter 250ms ...; }
@keyframes enter { from { opacity: 0; transform: translateY(8px); } to { ... } }
```

动画不跑（后台标签 / 被禁用）→ 照样看得见；跑了 → 照常淡入。
**公开页不能把可见性押在动画上。**

### ② 隐藏 `.proto-picker` 的规则被后面的 style 块覆盖

原型有**两个** `<style>`：第二个是「Picker 样式（逐字照抄）」，它把
`.proto-picker` 重定义成 `display: flex`。同特异度 → **后写的赢** →
我加在第一个 style 块末尾的 `display: none` 被它吃掉了，切换器照样显示。

改法：生产化 CSS 放在**最后一个 style 块之后**，并加 `!important` 双保险。

## 下载入口：为什么不再写死版本号（2026-10-23）

### 事故

页面的下载按钮写死 `tingxia-0.3.1.apk`（6 处：3 个方向的 CTA × 主链接 + IP 兜底），
版本号文字写死 `v0.3.1`（3 处）。而**发版流程完全不知道营销页的存在** ——
发版 = 上传 APK + 改 env + 重启服务，三步都不碰这个静态页。

结果：服务上已经是 0.3.6，页面还写着 0.3.1，**中间漏了 5 个版本**。
而且是「看起来正常」—— 链接还能点，下载下来的却是一个五个月前的包。

### 根因

不是「忘了改」，是**「当前版本」没有唯一真相源**：

| 事实 | 原来存在哪 |
|---|---|
| APK 文件 | `/var/www/stashbox-downloads/tingxia-<ver>.apk` |
| 版本号（接口读的） | `/etc/stashbox/stashbox.env` |
| 版本号（页面显示的） | 营销页 HTML 里写死 |

三处各说各的，谁也不校验谁。靠「记得改」来维持一致，必然漏。

### 现在的分工

| 事实 | 唯一真相源 | 谁维护 |
|---|---|---|
| 当前版本号 | `stashbox.env` 的三个 `STASHBOX_APP_*` 变量 | `deploy/release_apk.sh` |
| 最新 APK | `/var/www/stashbox-downloads/latest.apk` → 软链 | `deploy/release_apk.sh` |
| 页面上的版本号文字 | 运行时从 `/api/v1/app/version` 取 | 无（自动） |

**下载链接不再随版本变**：页面指向稳定路径 `/downloads/latest.apk`，
nginx 的 `/downloads/` 是 `alias /var/www/stashbox-downloads/`，
里面有个 `latest.apk` 软链指向当前版本。页面**一次都不用再改**。

**版本号文字运行时取**：`/api/` 走网关，与页面**同源**（无 CORS 问题），
所以页面可以直接 fetch `/api/v1/app/version`。
取不到时（无 JS / 爬虫 / 接口挂了）占位留空 → 文案变成
「Android 内测版 · iOS 规划中」—— **不含版本号就不会过期、不会说谎**。

### 发版脚本

`backend/deploy/release_apk.sh` 一次做完：上传 APK → 更新 `latest.apk` 软链 →
改 env → 重启 user-service → **校验**（哈希、软链指向、版本接口、实际下载大小）。

校验不是装饰：软链指错、env 写错、上传了半个文件，这三种错在
「页面上看起来一切正常」时最难发现。

```bash
./backend/deploy/release_apk.sh <apk 路径> <版本名> <versionCode>
```

---

## 部署

```bash
# 服务器上的 web 根
/var/www/stashbox-site/index.html

# nginx 路由见 backend/deploy/patch-nginx-site.py
#   stashbox.cn / www.stashbox.cn  → 本目录（/api/ /audio/ /downloads/ 走网关）
#   admin.stashbox.cn              → 后台 SPA
#   8.148.9.151                    → 后台 SPA（IP 回退路径）
```

⚠️ **后台和公开页不同源**是刻意的：后台登录态在 localStorage
（`AuthGuard` → `/login`），同源等于把后台 token 暴露给公开页。

## 改完怎么发布

```bash
# 1) 改源码（原型或本文件）
python3 /tmp/build_landing.py <原型路径> infra/web/index.html   # 重新生成
# 2) 传到服务器
rsync -a infra/web/index.html root@8.148.9.151:/var/www/stashbox-site/index.html
# 3) reload（nginx 不用改，除非路由变了）
ssh root@8.148.9.151 'systemctl reload nginx'
```

营销页的 html 是 `no-cache` 的（nginx 里 `location = /index.html`），
改完 reload 就能看到，不用清 CDN。
