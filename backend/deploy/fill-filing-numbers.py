#!/usr/bin/env python3
"""把备案号注入到两个站点的页脚。

## 为什么要有这个脚本

备案号必须出现在**两个**站点的页脚：
  · `stashbox.cn`        —— 营销页（`infra/web/index.html`）
  · `admin.stashbox.cn` —— 后台 SPA（`stashbox-admin-web/src/components/Layout.tsx`）

而这两处在**两个不同的 git 仓**里。号码写两遍 = 迟早只改一处
= 公安备案审核时「网站底部没有备案号」被驳回。

所以号码只存在**一个**地方：`infra/web/filing.json`。

## 关键设计：源码留占位符，注入只发生在**发布产物**上

一开始我把注入做成了「直接改源码」，结果踩了个坑：
第一次注入把源码里的 `{{ICP_NUMBER}}` 替换成了真号（或空串），
**占位符被消耗掉了** → 下次再填号时，脚本找不到占位符，**填不进去**。

所以现在：
  · 源码里**永远**是 `{{ICP_NUMBER}}` / `{{PSB_NUMBER}}`；
  · 注入产出**发布产物**（`infra/web/dist/index.html`），源码不动；
  · admin-web 走**构建期**注入（`VITE_ICP_NUMBER` / `VITE_PSB_NUMBER`），
    bundle 由 vite 产出，同样不动源码。

这样「改号 → 重新注入 → 重新发布」可以反复做，幂等。

## 号码为空时怎么办

ICP 备案通过前号码不存在（空串）。
这时**整段页脚不输出** —— 页面不能出现一个看着像真号、实际是占位的字符串，
那是骗人；而审核看到空页脚和看到假号同样会被驳回（但假号还可能被当成
「乱填」）。等号码到位再注入、发布，是唯一正确的顺序。

## 用法

    # 1) 改号码（唯一入口）
    #    编辑 infra/web/filing.json 的 icp / psb

    # 2) 生成营销页发布产物
    python3 backend/deploy/fill-filing-numbers.py --emit-landing

    # 3) 构建 admin-web（号码经环境变量注入）
    python3 backend/deploy/fill-filing-numbers.py --print-admin-env \\
        --admin-repo ../stashbox-admin-web
    #    → 输出 VITE_ICP_NUMBER=... VITE_PSB_NUMBER=...，带进 pnpm build

    # 4) 校验两处是否都带上了
    python3 backend/deploy/fill-filing-numbers.py --check \\
        --admin-repo ../stashbox-admin-web
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LANDING_SRC = REPO_ROOT / "infra/web/index.html"
LANDING_OUT = REPO_ROOT / "infra/web/dist/index.html"
FILING = REPO_ROOT / "infra/web/filing.json"
DEFAULT_ADMIN_REPO = REPO_ROOT.parent / "stashbox-admin-web"
ADMIN_LAYOUT = Path("src/components/Layout.tsx")

FOOTER_BLOCK = re.compile(
    r"\n<!-- 备案号页脚.*?</footer>\n",
    re.S,
)
ICP_LINK = re.compile(
    r'\s*<a href="https://beian\.miit\.gov\.cn"[^>]*data-icp-number>\{\{ICP_NUMBER\}\}</a>'
)
PSB_LINK = re.compile(
    r'\s*<a href="https://beian\.mps\.gov\.cn"[^>]*data-psb-number>\{\{PSB_NUMBER\}\}</a>'
)
SEPARATOR = re.compile(r'\s*<span class="site-footer-sep"[^>]*>·</span>')
SITE_NAME_SPAN = re.compile(
    r'\s*<span class="site-footer-name"[^>]*data-site-name>\{\{SITE_NAME\}\}</span>'
)
FILING_ROW = re.compile(r'\s*<p class="site-footer-filing"[^>]*>\{\{FILING_LINKS\}\}</p>')
COPYRIGHT_SPAN = re.compile(r'\s*<p class="site-footer-copyright"[^>]*>\{\{COPYRIGHT\}\}</p>')
PSB_ICON_IMG = re.compile(r'\s*<img class="site-footer-psb-icon"[^>]*data-psb-icon>')


PSB_ICON_PATH = REPO_ROOT / "infra/web/images/psb-icon.png"


def psb_icon_available() -> bool:
    """公安备案官方图标是否已就位（从阿里云下载后放到 infra/web/images/）。"""
    return PSB_ICON_PATH.exists()


def load_numbers() -> tuple[str, str, str, str]:
    """返回 (site_name, copyright_text, icp, psb)。都来自同一个 filing.json。"""
    data = json.loads(FILING.read_text(encoding="utf-8"))
    holder = (data.get("copyright_holder") or "").strip()
    year = str(datetime.now().year)
    copyright_text = f"© {year}{holder}. All rights reserved." if holder else ""
    return (
        (data.get("site_name") or "").strip(),
        copyright_text,
        (data.get("icp") or "").strip(),
        (data.get("psb") or "").strip(),
    )


def render_landing(
    site_name: str, copyright_text: str, icp: str, psb: str, psb_icon: str = ""
) -> str:
    """把源码渲染成发布产物：替换占位符；空号则整段页脚不输出。

    工信部要求页脚**同时**标明「网站名称」和备案号（两者都要与备案系统一致），
    少一行就是「备案信息不完整」。
    """
    text = LANDING_SRC.read_text(encoding="utf-8")

    if copyright_text:
        text = text.replace("{{COPYRIGHT}}", copyright_text)
    else:
        text = COPYRIGHT_SPAN.sub("", text)

    if site_name:
        text = text.replace("{{SITE_NAME}}", site_name)
    else:
        text = SITE_NAME_SPAN.sub("", text)

    # 备案号那一行**整段生成**（而不是逐个链接替换）——
    # 逐个替换在「有一边没号」时会留下孤零零的分隔符或空标签。
    links = []
    if icp:
        links.append(
            f'<a href="https://beian.miit.gov.cn" target="_blank" rel="noreferrer noopener" '
            f"data-icp-number>{icp}</a>"
        )
    if psb:
        # 公安备案图标必须是阿里云「下载备案编号图标」给的**官方那张**。
        # 没有官方图标就不显示图标 —— 自造一个盾牌等于伪造官方标识。
        icon = (
            '<img class="site-footer-psb-icon" src="/images/psb-icon.png" alt="" aria-hidden="true">'
            if psb_icon
            else ""
        )
        links.append(
            f'<span class="site-footer-sep">·</span>'
            f'<a class="site-footer-psb" href="https://beian.mps.gov.cn" target="_blank" '
            f'rel="noreferrer noopener" data-psb-number>{icon}<span>{psb}</span></a>'
        )
    filing_html = "\n    ".join(links)

    if filing_html:
        text = text.replace("{{FILING_LINKS}}", filing_html)
    else:
        text = FILING_ROW.sub("", text)

    # 三个号都没有 → 整个页脚不输出
    if not icp and not psb and not site_name and not copyright_text:
        text = FOOTER_BLOCK.sub("\n", text)

    return text


def emit_landing(
    site_name: str, copyright_text: str, icp: str, psb: str, psb_icon: str = ""
) -> Path:
    LANDING_OUT.parent.mkdir(parents=True, exist_ok=True)
    LANDING_OUT.write_text(
        render_landing(site_name, copyright_text, icp, psb, psb_icon), encoding="utf-8"
    )
    return LANDING_OUT


def check(site_name: str, copyright_text: str, icp: str, psb: str, admin_repo: Path) -> int:
    """两个站点的发布产物是否都带了号码、且链接正确。"""
    problems = []

    # ① 营销页：看**源码**的占位符 + 产物的内容
    src = LANDING_SRC.read_text(encoding="utf-8")
    if "{{SITE_NAME}}" not in src or "{{ICP_NUMBER}}" not in src or "{{PSB_NUMBER}}" not in src:
        problems.append("营销页源码的占位符被改掉了（源码必须永远保留占位符）")

    out = LANDING_OUT if LANDING_OUT.exists() else None
    if out is None:
        problems.append("营销页发布产物还没生成（跑 --emit-landing）")
    else:
        html = out.read_text(encoding="utf-8")
        if site_name:
            if site_name not in html:
                problems.append(f"营销页产物缺网站名称 {site_name}")
        if copyright_text:
            if copyright_text not in html:
                problems.append(f"营销页产物缺版权行 {copyright_text}")
        if icp:
            if icp not in html:
                problems.append(f"营销页产物缺 ICP 备案号 {icp}")
            if "beian.miit.gov.cn" not in html:
                problems.append("营销页的 ICP 号没链到 beian.miit.gov.cn")
        if psb:
            if psb not in html:
                problems.append(f"营销页产物缺公安备案号 {psb}")
            if "beian.mps.gov.cn" not in html:
                problems.append("营销页的公安号没链到 beian.mps.gov.cn")

    # ② admin-web：看源码用的是构建期注入
    admin_path = admin_repo / ADMIN_LAYOUT
    if admin_path.exists():
        admin = admin_path.read_text(encoding="utf-8")
        if "VITE_ICP_NUMBER" not in admin:
            problems.append("admin-web 没改成构建期注入（VITE_ICP_NUMBER）")
        if "{{ICP_NUMBER}}" in admin:
            problems.append("admin-web 源码里还有占位符（应该用构建期注入）")
        if site_name and site_name not in admin:
            problems.append(f"admin-web 缺网站名称 {site_name}")
    else:
        problems.append(f"找不到 admin-web 的 {admin_path}")

    if problems:
        print("✗ 备案号检查未通过：")
        for p in problems:
            print(f"    · {p}")
        return 1
    print("✓ 两个站点的备案号链路都就位")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-landing", action="store_true", help="生成营销页发布产物")
    ap.add_argument("--print-admin-env", action="store_true", help="输出 admin 构建用的环境变量")
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    ap.add_argument("--admin-repo", default=str(DEFAULT_ADMIN_REPO))
    args = ap.parse_args()
    admin_repo = Path(args.admin_repo).resolve()

    site_name, copyright_text, icp, psb = load_numbers()
    if not site_name:
        print("! filing.json 的 site_name 为空 —— 页脚的网站名称行不显示")
    if not icp:
        print("! filing.json 的 icp 为空 —— 页脚的 ICP 段整段不显示（宁缺勿假）")
    if not psb:
        print("! filing.json 的 psb 为空 —— 页脚的公安段整段不显示（公安备案通过后才有）")

    if args.print_admin_env:
        # 供 `pnpm build` 使用的环境变量（值可能为空，空则不注入）
        # 输出**可直接 eval 的 shell 片段**（值用单引号包起来）——
        # 版权行里有空格（"© 2026听匣. All rights reserved."），
        # 用空格分隔的 KEY=VALUE 直接 `env $X pnpm build` 会被拆成多个参数。
        parts = []
        for key, value in (
            ("VITE_SITE_NAME", site_name),
            ("VITE_COPYRIGHT", copyright_text),
            ("VITE_ICP_NUMBER", icp),
            ("VITE_PSB_NUMBER", psb),
            ("VITE_PSB_ICON", "/images/psb-icon.png" if psb_icon_available() else ""),
        ):
            if value:
                parts.append(f"export {key}='{value}'")
        print("\n".join(parts))
        return 0

    if args.emit_landing:
        out = emit_landing(site_name, copyright_text, icp, psb, "1" if psb_icon_available() else "")
        print(f"✓ 营销页发布产物：{out}")
        return 0

    if args.check:
        return check(site_name, copyright_text, icp, psb, admin_repo)

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
