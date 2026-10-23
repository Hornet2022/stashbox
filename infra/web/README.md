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

### 「IP 直连」那个备用下载入口：已删（2026-10-23）

原来 CTA 下面有两个下载入口：主按钮 + 一个「走 IP 直连下载（不经过域名）」。
它现在**有害**，删了。

它的原始理由（2026-10-22 实测）当时是成立的：域名入口**直出**，
有个内测用户反馈「下载不了」，日志显示

```
Chrome/135            GET /downloads/…apk  200  229376    ← 只传了 224KB 就断
AndroidDownloadManager GET /downloads/…apk  200/206 ×5    ← 靠 Range 续传下完 3.8MB
```

服务器侧全清白（curl 全速下完、Range 206 正常），是那个用户的网络在**大流量**上
掐断了连接 —— 而域名直出，所以给一个 IP 入口当兜底，还能顺带区分病因。

**但那个前提没了**：域名入口现在是 **302 到 IP**（备案未过期的既有设计，
见 nginx 的 `stashbox-downloads-redirect.conf`）。于是：

| 原设计的前提 | 现状 |
|---|---|
| 域名直出，IP 是另一条路 | 域名 302 → IP，**主按钮本来就落到 IP 入口** |
| 「IP 直连」能绕过域名问题 | 断流**就发生在 IP 入口上**（上面那条 224KB 就是 302 之后的 IP 请求） |
| 「IP 能下完 = 域名线路问题」 | 两条路现在是同一条，判据失效 |

留着它还有害：它把用户引向「域名有问题」的错误归因，而真实病因是
**当前网络掐大流量连接**。

所以现在只留**有证据**的那条建议：「用系统下载器打开」——
系统下载器会用 Range 续传（实测靠 206 把 3.8MB 下完了，Chrome 只下了 224KB）。
文案同时改成说准现象和归因：

> 下载卡在几百 KB 不动？多半是当前网络把大流量连接掐断了。试试**用系统下载器打开**：
> 长按上面的按钮 → 复制链接地址 → 粘到系统下载器，它支持断点续传。

（长按复制得到的是**绝对 URL**，可以直接粘进系统下载器；页面上的 href 是相对路径
不影响这一点。）

### 发版脚本

`backend/deploy/release_apk.sh` 一次做完：上传 APK → 更新 `latest.apk` 软链 →
改 env → 重启 user-service → **校验**（哈希、软链指向、版本接口、实际下载大小）。

校验不是装饰：软链指错、env 写错、上传了半个文件，这三种错在
「页面上看起来一切正常」时最难发现。

```bash
./backend/deploy/release_apk.sh <apk 路径> <版本名> <versionCode>
```

---

## 排版验收（2026-10-23）

营销页的硬要求是**一屏放下、不滚动** —— 页面要滚一屏就说明首屏没装下 CTA。
这个要求肉眼很难验准（不同视口、不同缩放、不同字体），所以用**像素量**。

```bash
# 工具在 admin-web 仓（整个工作区只有那里装了 playwright）
cd ../stashbox-admin-web
node scripts/measure-landing.mjs ../stashbox/infra/web/index.html [--shots]
```

它量三件事，任何一项不达标就不算通过：

1. **是否一屏** —— 文档高 ≤ 视口高；
2. **CTA 与页脚是否在首屏内** —— 转化目标与合规信息都不能被推到第二屏；
3. **文字与分隔线的最小间距** —— 小于 4px 视为「压线」。

覆盖 10 个视口：1920×1080 / 1440×900 / 1366×768 / 1280×720 / 768×1024（平板竖屏）/
412×915 / 390×844 / 360×640 / 320×568（超窄）/ 844×390（手机横屏）。

当前状态：**9/10 一屏**（手机横屏仍会滚动 66px —— 横屏不是目标场景，
且下载按钮仍在首屏内）。

### 一屏是怎么做到的

| 手段 | 说明 |
|---|---|
| 高度分配在 **body** 一层做 | `body` 是 flex column：`#stage` 拿走除页脚外的全部高度，页脚按内容高度贴底。**页脚是 body 的直接子级**（在 `#stage` 外面），早先它被挤到视口外（实测文档高 1043 / 视口 900） |
| 弹性留白由**标题区**吸收 | `.headline` 是 `flex: 1 1 auto`，`.cols` 是 `flex: 0 0 auto`（三栏是内容，高度由文字决定） |
| 尺寸随**视口高度**自适应 | 关键间距与标题字号都用 `clamp()` 配 `vh`；标题字号还同时受 `vw` 与 `vh` 约束（`min(8.2vw, 9.4vh)`）—— 只按宽度算的话，矮屏上标题一行就顶掉半屏 |
| 手机竖屏三栏**横排** | 竖排三段要占 300px+，横排只占 ~150px，一屏才装得下；且保住了「三栏并列」这个设计语言 |
| 文字与分隔线留出间隙 | `.col p { padding-right: 12px }` —— 之前栏的 padding-right 只有 24px，窄屏时几乎为零（实测 4~6px），文字行末会贴到线上 |

### 一个容易踩的坑

父级改成 flex column 之后，子项的 `margin: 0 auto` 会让它**收缩到内容宽度并居中**
（flex 里的 auto margin 会吃掉 `stretch`）—— masthead 的「听匣」会跑到页面中间。
所以 flex 子项要显式 `width: 100%`。

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
