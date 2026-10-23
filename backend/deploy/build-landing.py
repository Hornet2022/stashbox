"""把 `~/.prototypes/tingxia-landing/index.html` 变成可上线的生产页。

原型 README 写着「状态：未决策、这只是探索，没有被提升为任何正式站点」。
这里做的是**把它变成一个真正的落地页**，改动都记在 README 里：

1. **默认方向 = 纸面**（方向 1），并且**把它的 markup 预渲染进 HTML**。
   理由：方向 2 的波形是 JS 生成的，方向 1 的是纯静态 —— 静态化之后
   ①无 JS 也能看到内容 ②微信/Twitter 的分享爬虫不跑 JS，卡片有内容
   （内测的主分享渠道就是微信，这是硬伤）③首屏不需要等 JS。
2. **方向切换器默认隐藏**（原型是三个方向并排让人挑的，对外是「没做完」的观感）。
   团队内部用 `?picker=1` 还能打开，`?v=1|2|3` 直接切方向。
3. **CTA 从「加入内测名单」改成「下载内测版」**—— 现在真的有可安装的
   release APK 了（/downloads/tingxia-0.3.0.apk），原型的 README 里
   「CTA 到底该是什么还没定」这个阻塞已经解除。
4. **方向 2 的样例音频换成真实成品**（原型里「《注意力经济下的深度阅读》·
   样例」是占位，没有音频源）。
5. 加 og: 标签（分享卡片的标题/描述），加 description meta。

用法：python3 build_landing.py <原型路径> <输出路径>

⚠️ 2026-10-23：原型 ~/.prototypes/tingxia-landing/index.html **已不存在**，
   本脚本当前**跑不起来**。infra/web/index.html 是现存的唯一真相源，
   改它请直接改（并同步本文件里的常量，否则将来原型找回来重新生成
   会把下载链接改回旧写法）。
"""

import re
import sys
from pathlib import Path

# 真实成品音频（生产库里 art_881ed5393d9d47248ed48271，227s）
SAMPLE_TITLE = "奇客Solidot｜《星际公民》前传《Squadron 42》完成功能开发"
SAMPLE_DUR = "03:47"
SAMPLE_PATH = "/audio/audio/art_881ed5393d9d47248ed48271.b7416ab1.mp3"
# 下载链接**不写死版本号**。
#
# 2026-10-23 实测的坑：这里写死成 v0.3.1，而发版流程（上传 APK / 改 env /
# 重启服务）**完全不知道营销页的存在** → 页面的下载入口一路停在 0.3.1，
# 服务上却已经是 0.3.6，中间漏了 5 个版本。根因是「当前版本」没有唯一真相源。
#
# 现在的分工：
#   · 下载链接 → 稳定路径 /downloads/latest.apk
#     （nginx 的 /downloads/ 是 alias 到 /var/www/stashbox-downloads/，
#      里面有一个 latest.apk 软链指向当前版本，由 deploy/release_apk.sh 维护）
#   · 版本号文字 → 运行时从同源 /api/v1/app/version 取，取不到就留空
#     （文案不含版本号 → 永不过时、不会说谎）
APK_PATH = "/downloads/latest.apk"
# 版本号的占位：页面运行时用接口返回的版本填这里。
# 静态渲染出来是空的 → 降级文案「Android 内测版 · iOS 规划中」。
APK_VER = '<span class="ver-slot" data-ver-slot></span>'

# ── 曾经的「IP 直连」备用入口：2026-10-23 删掉了 ──────────────
#
# 原来有第二个下载链接「走 IP 直连下载（不经过域名）」，它的理由是：
# 当时**域名入口是直出的**，所以给一个 IP 入口当兜底，还能顺带区分病因
# （IP 能下完 = 域名线路问题；IP 也断 = 大流量 QoS）。
#
# 现在这个前提没了 —— 域名入口改成 **302 到 IP**（备案未过期的既有设计，
# 见 nginx 的 stashbox-downloads-redirect.conf）。于是：
#   · 主按钮点下去**本来就落到 IP 入口** → 「IP 直连」这个链接不绕过任何东西；
#   · 断流**就发生在 IP 入口上**（2026-10-23 07:13 实测：Chrome/135 经 302 到 IP
#     后只传了 224KB 就断）→ 它给的"解法"根本不解决断流；
#   · 那套「IP 能下完 = 域名线路问题」的判据也一并失效（两条路现在是同一条）。
#
# 留着它还有害：它把用户引向「域名有问题」的错误归因，而真实病因是
# **当前网络掐大流量连接**。所以删掉，只留有证据的那条建议
# ——「用系统下载器打开」（系统下载器会用 Range 续传，2026-10-23 07:13 实测
# 它靠 206 把 3.8MB 下完了，而 Chrome 只下了 224KB）。


def extract_template(src: str, func_name: str) -> str:
    """从 `function f() { return `...`; }` 里取出模板字符串的内容。"""
    m = re.search(rf"function {func_name}\(\)\s*\{{\s*return\s*`(.*?)`;\s*\}}", src, re.S)
    if not m:
        raise SystemExit(f"没找到 {func_name} 的模板")
    return m.group(1)


def main() -> int:
    proto_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    src = proto_path.read_text(encoding="utf-8")

    # ── 1) 标题与 meta ───────────────────────────────────────────
    src = src.replace(
        "<title>听匣 · 首页方向探索</title>",
        "<title>听匣 · 把读不完的，变成听得完的</title>\n"
        '<meta name="description" content="剪藏一条链接，AI 把它蒸馏成一段刚好能在通勤路上听完的音频。'
        '内容消费的 AI 翻译机，Android 内测中。">\n'
        '<meta property="og:type" content="website">\n'
        '<meta property="og:title" content="听匣 · 把读不完的，变成听得完的">\n'
        '<meta property="og:description" content="剪藏一条链接，AI 把它蒸馏成一段刚好能在通勤路上听完的音频。Android 内测中。">\n'
        '<meta name="theme-color" content="#F5F2EB">',
    )

    # ── 2) 预渲染默认方向（纸面）到 #stage ────────────────────────
    broadsheet = extract_template(src, "vBroadsheet")
    # 模板里没有 ${}，可以直接当静态 HTML 用
    assert "${" not in broadsheet, "纸面方向里出现了模板插值，需要改成先渲染再注入"
    src = src.replace('<div id="stage"></div>', f'<div id="stage">\n{broadsheet}\n</div>')

    # ── 3) CTA：加入内测名单 → 下载内测版 ────────────────────────
    src = src.replace(
        '<p class="cta-note">当前处于内测阶段，Android 优先，iOS 规划中。</p>\n'
        '      <button class="btn">加入内测名单</button>',
        '<p class="cta-note">Android 内测版' + APK_VER + " · iOS 规划中</p>\n"
        f'      <a class="btn" href="{APK_PATH}" download>下载内测版</a>\n'
        '      <p class="cta-note" style="margin-top:10px">'
        "下载卡在几百 KB 不动？多半是当前网络把大流量连接掐断了。"
        "试试<strong>用系统下载器打开</strong>：长按上面的按钮 → 复制链接地址 → "
        "粘到系统下载器，它支持断点续传。</p>",
    )
    src = src.replace(
        "<p>内测中 · Android 优先</p>\n" '        <button class="btn">加入内测名单</button>',
        f"<p>Android 内测版{APK_VER} · iOS 规划中</p>\n"
        f'        <a class="btn" href="{APK_PATH}" download>下载内测版</a>\n'
        '        <p class="cta-note" style="margin-top:10px">'
        "下载卡在几百 KB 不动？多半是当前网络把大流量连接掐断了。"
        "试试<strong>用系统下载器打开</strong>：长按上面的按钮 → 复制链接地址 → "
        "粘到系统下载器，它支持断点续传。</p>",
    )
    if "加入内测名单" in src:
        raise SystemExit("还有没换掉的「加入内测名单」")

    # ── 4) 方向 2：真样例音频 ───────────────────────────────────
    src = src.replace(
        '<p class="t">《注意力经济下的深度阅读》· 样例</p>\n'
        '        <p class="s">05:00 · 已蒸馏</p>',
        f'<p class="t">{SAMPLE_TITLE}</p>\n'
        f'        <p class="s">{SAMPLE_DUR} · 真实内测成品</p>',
    )
    # 播放按钮改成真音频
    src = src.replace(
        '</div>\n    <div class="chain enter"',
        f'</div>\n    <audio id="sampleAudio" preload="none" src="{SAMPLE_PATH}"></audio>\n'
        '    <div class="chain enter"',
    )

    # ── 5) 入场动效不能决定「内容能不能看见」 ─────────────────────
    #
    # 原型的写法是：
    #     .enter { opacity: 0; animation: enter 250ms ... forwards; }
    #     @keyframes enter { to { opacity: 1; transform: none; } }
    #
    # 这有个真问题：**opacity: 0 是基础样式**，只有动画跑完
    # （且 fill-mode forwards 把 1 定住）才看得见。浏览器在**后台标签页**
    # 会暂停 CSS 动画 → 动画停在 0% → forwards 把 opacity: 0 定住 →
    # **整页白**。公开页不能把可见性押在动画上。
    #
    # 改成：基础样式就是可见的，动画只是「淡入」这个增强。
    # 动画不跑（后台标签/被禁用）→ 照样看得见；跑了 → 照常淡入。
    old_enter = """.enter {
  opacity: 0;
  transform: translateY(8px);
  animation: enter 250ms var(--ease-smooth-out) forwards;
}
@keyframes enter { to { opacity: 1; transform: none; } }"""
    new_enter = """.enter {
  /* 默认可见 —— 动效只是增强，不能让内容消失 */
  opacity: 1;
  transform: none;
  animation: enter 250ms var(--ease-smooth-out);
}
@keyframes enter {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: none; }
}"""
    if old_enter not in src:
        raise SystemExit("没找到 .enter 规则（原型结构变了？）")
    src = src.replace(old_enter, new_enter)

    # ── 6) 切换器默认隐藏 + 链接样式（放最后，压过 picker 样式块）──
    # ⚠️ 必须放在**最后**：原型有两个 <style>，第二个是「Picker 样式逐字照抄」，
    #    它把 .proto-picker 重定义成 display:flex —— 放在前面会被它覆盖掉
    #    （同样特异度，后写的赢）。
    css_add = """
/* ═══ 生产化改动（见 infra/web/README.md）═══ */
/* 方向切换器：原型是「三个方向并排挑」的探索工具，对外不该出现。
   团队内部用 ?picker=1 打开。 */
.proto-picker { display: none !important; }
body.show-picker .proto-picker { display: flex !important; }
/* CTA 从 button 换成 <a>：去掉下划线，其余沿用 .btn 的类样式 */
a.btn { text-decoration: none; }
"""
    src = src.replace("</style>", css_add + "</style>")

    # ── 7) JS：?picker=1 显示切换器；方向 2 播真音频 ─────────────
    #
    # ⚠️ 原型的 setActive() 会把当前方向写进地址栏（history.replaceState）
    #    → 生产页每次打开都变成 /?v=1，**分享出去的链接带着这个噪音**。
    #    改成：只有在「探索模式」（?picker=1）下才写 URL。
    src = src.replace(
        """  const url = new URL(location);
  url.searchParams.set('v', i + 1);
  history.replaceState(null, '', url);
  mount(i);""",
        """  // 只在探索模式下写地址栏 —— 生产页的分享链接不该带 ?v=1
  if (document.body.classList.contains('show-picker')) {
    const url = new URL(location);
    url.searchParams.set('v', i + 1);
    history.replaceState(null, '', url);
  }
  mount(i);""",
    )
    src = src.replace(
        "setActive((parseInt(new URLSearchParams(location.search).get('v'), 10) || 1) - 1);",
        "if (new URLSearchParams(location.search).get('picker') === '1') "
        "document.body.classList.add('show-picker');\n"
        "setActive((parseInt(new URLSearchParams(location.search).get('v'), 10) || 1) - 1);",
    )
    src = src.replace(
        """function wireWave() {
  const play = document.getElementById('wavePlay');
  const bars = [...document.querySelectorAll('#waveCanvas .bar')];
  if (!play) return;
  let on = false;
  play.addEventListener('click', () => {
    on = !on;
    bars.forEach((b, i) => b.classList.toggle('playing', on));
    bars.forEach((b, i) => b.classList.toggle('lit', on && i > 8 && i < 54));
    play.textContent = on ? '❚❚' : '▶';
    play.setAttribute('aria-label', on ? '暂停样例' : '播放样例');
  });
}""",
        """function wireWave() {
  const play = document.getElementById('wavePlay');
  const bars = [...document.querySelectorAll('#waveCanvas .bar')];
  const audio = document.getElementById('sampleAudio');
  if (!play) return;
  // 真音频：播到哪，波形亮到哪 —— 波形不是装饰，是进度条
  if (audio) {
    audio.addEventListener('timeupdate', () => {
      const p = audio.duration ? audio.currentTime / audio.duration : 0;
      const n = bars.length;
      bars.forEach((b, i) => b.classList.toggle('lit', i / n <= p));
    });
    audio.addEventListener('ended', () => {
      bars.forEach((b) => b.classList.remove('lit'));
      play.textContent = '▶';
      play.setAttribute('aria-label', '播放样例');
    });
  }
  play.addEventListener('click', () => {
    if (!audio) return;
    if (audio.paused) { audio.play(); play.textContent = '❚❚'; play.setAttribute('aria-label', '暂停样例'); }
    else { audio.pause(); play.textContent = '▶'; play.setAttribute('aria-label', '播放样例'); }
  });
}""",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(src, encoding="utf-8")
    print(f"已生成 {out_path}（{len(src)} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
