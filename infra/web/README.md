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
