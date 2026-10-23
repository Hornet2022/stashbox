#!/usr/bin/env python3
"""make_logo.py — 「听匣」logo 资产生成器（唯一真相源）。

## 为什么字形要抽成路径，而不是直接用字体

logo 要出现在**没有装这个字体**的地方：网页（别人的浏览器）、
Android 图标（系统按路径渲染，与字体无关）、favicon、社交分享卡片。
用字体渲染的图在缺字体的机器上会掉字或变成方块 —— 品牌资产不能这样。

所以这里用 fontTools 把「听」「匣」两个字形**抽成矢量路径**，
再从同一份路径数据导出：
  - 网页 SVG / PNG
  - Android VectorDrawable（前景图）与各密度 mipmap（老系统）
一份路径 → 两端一致。

## 字体

Songti SC（macOS `/System/Library/Fonts/Supplemental/Songti.ttc`）——
与落地页 `--serif` 字体栈（'Noto Serif SC', 'Songti SC', 'SimSun', Georgia）
同源，所以 logo 与正文字体是同一种宋体气质。

## 色彩（取自落地页的令牌层，勿另造）

    --ink        #1A1A1A   字形
    --cream      #F5F2EB   纸底
    --warm-ochre #A67B5B   唯一的点缀色（一条压线）

## 两个切面（各有各的用处，别混用）

  字标 wordmark  横排「听匣」，透明底   → 网页页眉/页脚、og 卡片
  标记 mark      竖排「听/匣」，纸底方块 → 启动器图标、favicon

为什么图标用竖排：横向两个字塞进方块里，单字高度只剩约 0.4 倍边长，
48dp 下笔画糊成一团；竖排后单字高度接近 0.5 倍边长，48dp 依然清晰。
实测圆形遮罩下，贴边的装饰（框、压线）会被切角或糊掉 → 图标只留字。

## 用法

    python3 make_logo.py                     # 生成网页资产 + 安卓资源
    python3 make_logo.py --preview           # 出一张多尺寸/多遮罩评估图
    python3 make_logo.py --android-res PATH  # 指定安卓 res 目录
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from fontTools.misc.transform import Transform
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTCollection

# ── 设计令牌（与 infra/web/index.html 的 :root 一致）─────────────────
INK = "#1A1A1A"
CREAM = "#F5F2EB"
CREAM_DEEP = "#EFE9DE"  # 纸底渐变的下端
OCHRE = "#A67B5B"

FONT_PATH = "/System/Library/Fonts/Supplemental/Songti.ttc"
FONT_FACE = "Songti SC Bold"  # 图标用 Bold：小尺寸笔画要撑得住

# 排版参数
TRACKING = 0.22  # 字距（em），与落地页 .wordmark 的 letter-spacing 一致
MARK_GAP = 0.085  # 竖排两字之间的间距（相对画布）

WORD = "听匣"


# ── 字体工具 ──────────────────────────────────────────────────────────
def _load_face(name: str = FONT_FACE):
    """从 .ttc 里按字体名挑出字面。"""
    coll = TTCollection(FONT_PATH, lazy=False)
    for f in coll.fonts:
        if f["name"].getDebugName(4) == name:
            return f
    raise SystemExit(f"字体里没有 {name}")


def _ink_box(font, char: str) -> tuple[float, float, float, float]:
    """字形的**墨迹**包围盒（font units, Y 向上）。

    为什么用墨迹盒而不是 em 盒：中文字形的墨迹只占 em 的 ~91%，
    按 em 排会让两个字看起来一大一小、上下不匀。
    """
    gs = font.getGlyphSet()
    bp = BoundsPen(gs)
    gs[font.getBestCmap()[ord(char)]].draw(bp)
    return bp.bounds


def _glyph_at(font, char: str, scale: float, left: float, bottom: float) -> str:
    """画一个字形：给定缩放，并让它的**墨迹左沿**落在 left、**墨迹下沿**落在 bottom。

    字体坐标 Y 向上 → 目标坐标 Y 向下，所以这里做一次翻转；
    翻转后的 path_d 可以直接喂给 Android VectorDrawable（那边也是 Y 向下）。
    """
    xmin, ymin, xmax, ymax = _ink_box(font, char)
    t = Transform(scale, 0, 0, -scale, left - scale * xmin, bottom + scale * ymin)
    gs = font.getGlyphSet()
    pen = SVGPathPen(gs)
    gs[font.getBestCmap()[ord(char)]].draw(TransformPen(pen, t))
    return pen.getCommands()


# ── 字标（横排，透明底）──────────────────────────────────────────────
def build_wordmark(size: float = 1.0, ink: str = INK) -> str:
    """横排「听匣」。viewBox **紧贴墨迹**。

    紧贴墨迹的意义：网页上用 `height: 1em` 缩放时，logo 的视觉高度
    就等于一个 CJK 字的墨迹高度，和原来的文字 wordmark 视觉对齐。
    """
    font = _load_face()
    upem = font["head"].unitsPerEm
    gap = size * TRACKING
    boxes = [_ink_box(font, ch) for ch in WORD]
    advances = [
        font.getGlyphSet()[font.getBestCmap()[ord(ch)]].width * size / upem
        for ch in WORD
    ]
    # 两个字统一到同一个墨迹高度（视觉等重）
    target_ink = size * max((b[3] - b[1]) for b in boxes) / upem
    total_w = sum(advances) + gap * (len(WORD) - 1)
    ink_bottom = min(b[1] for b in boxes)
    ink_h = target_ink

    paths = []
    x = 0.0
    for ch, adv, box in zip(WORD, advances, boxes):
        s = target_ink / (box[3] - box[1])
        # 以**字的推进原点** x 落位（与文字排版的字距语义一致），
        # 墨迹左沿 = x + xmin*s
        paths.append(
            f'<path d="{_glyph_at(font, ch, s, x + box[0] * s, ink_bottom)}"/>'
        )
        x += adv + gap

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w:.3f} {ink_h:.3f}" '
        f'width="{total_w:.3f}" height="{ink_h:.3f}" role="img" aria-label="听匣">'
        # 墨迹整体在基线之下（底沿 ≈ -0.17em），要整体下移，让墨迹正好填满 viewBox
        f'<g fill="{ink}" transform="translate(0,{ink_h - ink_bottom:.3f})">{"".join(paths)}</g></svg>'
    )


# ── 标记（竖排，纸底方块）────────────────────────────────────────────
def build_mark(ink: str = INK, rule: str = OCHRE, variant: str = "plain") -> str:
    """竖排「听/匣」。1024 见方画布，坐标即像素。

    variant:
      plain  只有两个字 —— **启动器图标用这个**。启动器会加圆形/圆角遮罩，
             贴边的装饰会被切角或糊掉（实测圆形遮罩下印章框会被切）。
      rule   字下加一条压线（报纸的报头线）—— 只给 og 卡片这类大尺寸场合。
      seal   外面套一个细印章框 —— 备选，不进产品。
    """
    canvas = 1024.0
    font = _load_face()

    boxes = [_ink_box(font, ch) for ch in WORD]
    ink_h = canvas * 0.285
    gap = canvas * MARK_GAP
    scales = [ink_h / (b[3] - b[1]) for b in boxes]
    widths = [(b[2] - b[0]) * s for b, s in zip(boxes, scales)]
    block_w = max(widths)
    block_h = ink_h * 2 + gap

    # 光学居中：字块中心略高于画布中心（视觉重心比几何中心高）
    top = (canvas - block_h) / 2 - canvas * 0.012

    paths = []
    for i, (ch, s, b) in enumerate(zip(WORD, scales, boxes)):
        # 以墨迹盒落位：左边沿居中，下沿对齐
        left = (canvas - (b[2] - b[0]) * s) / 2
        bottom = top + ink_h * (i + 1) + gap * i
        paths.append(f'<path d="{_glyph_at(font, ch, s, left, bottom)}"/>')

    parts = [
        f'<rect width="{canvas:.0f}" height="{canvas:.0f}" fill="url(#paper)"/>',
        f'<g fill="{ink}">{"".join(paths)}</g>',
    ]
    if variant == "rule":
        rw = block_w * 0.92
        parts.append(
            f'<rect x="{(canvas - rw) / 2:.2f}" y="{top + block_h + canvas * 0.055:.2f}" '
            f'width="{rw:.2f}" height="{canvas * 0.022:.2f}" fill="{rule}"/>'
        )
    elif variant == "seal":
        inset, sw, radius = canvas * 0.115, canvas * 0.017, canvas * 0.045
        parts.append(
            f'<rect x="{inset:.2f}" y="{inset:.2f}" width="{canvas - 2 * inset:.2f}" '
            f'height="{canvas - 2 * inset:.2f}" rx="{radius:.2f}" ry="{radius:.2f}" '
            f'fill="none" stroke="{rule}" stroke-width="{sw:.2f}"/>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {canvas:.0f} {canvas:.0f}" '
        f'width="{canvas:.0f}" height="{canvas:.0f}" role="img" aria-label="听匣">'
        f'<defs><linearGradient id="paper" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{CREAM}"/><stop offset="1" stop-color="{CREAM_DEEP}"/>'
        f"</linearGradient></defs>" + "".join(parts) + "</svg>"
    )


def build_favicon(ink: str = INK) -> str:
    """favicon 专用：64 见方的同一枚标记，但字块占得更满。

    为什么不能直接用 mark：mark 的字块占画布 65%（1024 见方），那是图标
    的比例 —— 塞进 16px 的浏览器标签页就只剩两团墨点。favicon 要占 ~78%。
    """
    canvas = 64.0
    font = _load_face()
    boxes = [_ink_box(font, ch) for ch in WORD]
    ink_h = canvas * 0.325
    gap = canvas * 0.075
    scales = [ink_h / (b[3] - b[1]) for b in boxes]
    block_h = ink_h * 2 + gap
    top = (canvas - block_h) / 2 - canvas * 0.012

    paths = []
    for i, (ch, s, b) in enumerate(zip(WORD, scales, boxes)):
        left = (canvas - (b[2] - b[0]) * s) / 2
        bottom = top + ink_h * (i + 1) + gap * i
        paths.append(f'<path d="{_glyph_at(font, ch, s, left, bottom)}"/>')

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {canvas:.0f} {canvas:.0f}" '
        f'width="{canvas:.0f}" height="{canvas:.0f}" role="img" aria-label="听匣">'
        f'<defs><linearGradient id="paper" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{CREAM}"/><stop offset="1" stop-color="{CREAM_DEEP}"/>'
        f'</linearGradient></defs>'
        f'<rect width="{canvas:.0f}" height="{canvas:.0f}" fill="url(#paper)"/>'
        f'<g fill="{ink}">{"".join(paths)}</g></svg>'
    )


# ── og 分享卡片 ───────────────────────────────────────────────────────
def build_og(width: int = 1200, height: int = 630) -> str:
    """社交分享卡片：纸底 + 字标 + 压线 + 口号 + 域名。

    版面按**真实墨迹盒**排（不是按 em 估）—— 估出来的间距会散。
    """
    word_size = 132.0
    wm = build_wordmark(size=word_size)
    # 从字标里取出内部路径（viewBox 紧贴墨迹，所以路径坐标就是墨迹坐标）
    inner = wm.split(">", 1)[1].rsplit("</svg>", 1)[0]
    g = wm.split('viewBox="', 1)[1].split('"', 1)[0].split()
    wm_w, wm_h = float(g[2]), float(g[3])

    rule_w = 150.0
    rule_gap = 46.0
    tag_gap = 46.0
    tag_size = 33.0
    total_h = wm_h + rule_gap + 3 + tag_gap + tag_size

    x = (width - wm_w) / 2
    y = (height - total_h) / 2 - 18
    rule_x = (width - rule_w) / 2
    rule_y = y + wm_h + rule_gap
    tag_y = rule_y + 3 + tag_gap + tag_size * 0.8

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}">'
        f'<rect width="{width}" height="{height}" fill="{CREAM}"/>'
        f'<rect x="0" y="0" width="{width}" height="9" fill="{INK}"/>'
        f'<g transform="translate({x:.2f},{y:.2f})">{inner}</g>'
        f'<rect x="{rule_x:.2f}" y="{rule_y:.2f}" width="{rule_w:.2f}" height="3" fill="{OCHRE}"/>'
        f'<text x="{width / 2:.0f}" y="{tag_y:.0f}" text-anchor="middle" '
        f'font-family="Songti SC, Noto Serif SC, serif" font-size="{tag_size:.0f}" '
        f'fill="{INK}">把读不完的，变成听得完的</text>'
        f'<text x="{width / 2:.0f}" y="{height - 52}" text-anchor="middle" '
        f'font-family="Menlo, monospace" font-size="19" letter-spacing="4" fill="#8C8377">STASHBOX.CN</text>'
        f"</svg>"
    )


# ── 导出 ──────────────────────────────────────────────────────────────
def rsvg(
    svg: str, out: Path, size: int | None = None, width: int | None = None
) -> None:
    """SVG → PNG（用 rsvg-convert，渲染质量比 PIL 稳）。

    size  = 正方形（图标）；width = 只给宽度，高度按比例（字标/卡片用）。
    ⚠️ 别对非正方形资产同时给 -w/-h —— 那会把字标压扁。
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["rsvg-convert", "-f", "png"]
    if size:
        cmd += ["-w", str(size), "-h", str(size)]
    elif width:
        cmd += ["-w", str(width)]
    cmd += ["-o", str(out), "-"]
    subprocess.run(cmd, input=svg.encode(), check=True)


def android_foreground(ink: str = INK) -> str:
    """Android 自适应图标的前景（VectorDrawable）。

    ⚠️ 不能直接用网页那套 1024 画布：
    自适应图标的画布是 108dp，其中**只有中央 72dp 一定可见**，
    圆形遮罩的安全区是直径 **66dp** 的圆。网页那套字块高占画布 65%
    （1024 → 70dp），**会被圆形遮罩切掉**。

    所以这里单独按 108dp 画布排：字块高 57dp，落在 66dp 的圆里
    （角点到圆心 ≈ 31dp < 33dp）。

    前景只写字：纸底是 adaptive icon 的 background（渐变 shape），
    压线/印章框那类装饰在这个尺寸会糊，不进前景。
    """
    vp = 108.0
    font = _load_face()
    boxes = [_ink_box(font, ch) for ch in WORD]
    ink_h = 26.0
    gap = 5.0
    scales = [ink_h / (b[3] - b[1]) for b in boxes]
    block_h = ink_h * 2 + gap
    top = (vp - block_h) / 2 - 0.5  # 视觉重心略高于几何中心

    paths = []
    for i, (ch, s, b) in enumerate(zip(WORD, scales, boxes)):
        left = (vp - (b[2] - b[0]) * s) / 2
        bottom = top + ink_h * (i + 1) + gap * i
        paths.append(
            '    <path\n        android:fillColor="%s"\n        android:pathData="%s" />'
            % (ink, _glyph_at(font, ch, s, left, bottom))
        )

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!-- 由 infra/web/assets/logo/make_logo.py 生成，勿手改。\n"
        "     前景只放字：纸底由 adaptive icon 的 background 负责。\n"
        "     字块高 57dp，落在 66dp 的圆形安全区内。 -->\n"
        '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
        '    android:width="108dp"\n'
        '    android:height="108dp"\n'
        '    android:viewportWidth="108"\n'
        '    android:viewportHeight="108">\n' + "\n".join(paths) + "\n</vector>\n"
    )


LEGACY_DENSITIES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def main() -> None:
    ap = argparse.ArgumentParser(description="生成「听匣」logo 资产")
    ap.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument(
        "--android-res",
        type=Path,
        default=None,
        help="安卓 res 目录（默认 ../stashbox-android/app/src/main/res）",
    )
    ap.add_argument(
        "--admin-public",
        type=Path,
        default=None,
        help="后台仓的 public 目录（默认 ../stashbox-admin-web/public）",
    )
    ap.add_argument("--preview", action="store_true", help="出多尺寸/多遮罩评估图")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    wordmark = build_wordmark()
    mark = build_mark()
    favicon = build_favicon()
    og = build_og()

    (out / "tingxia-wordmark.svg").write_text(wordmark, encoding="utf-8")
    (out / "tingxia-mark.svg").write_text(mark, encoding="utf-8")
    (out / "tingxia-favicon.svg").write_text(favicon, encoding="utf-8")
    (out / "tingxia-og.svg").write_text(og, encoding="utf-8")
    rsvg(wordmark, out / "tingxia-wordmark.png", width=512)
    for s in (32, 64, 128, 180, 192, 512, 1024):
        rsvg(mark, out / f"tingxia-mark-{s}.png", s)
    for s in (16, 32, 48):
        rsvg(favicon, out / f"tingxia-favicon-{s}.png", s)
    rsvg(og, out / "tingxia-og.png", width=1200)

    if args.preview:
        from PIL import Image, ImageDraw

        def circle(im):
            size = im.size[0]
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            out_ = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            out_.paste(im.convert("RGBA"), (0, 0), mask)
            return out_

        big = Image.open(out / "tingxia-mark-1024.png").convert("RGBA")
        rows = []
        for size in (256, 96, 48, 32):
            sq = big.resize((size, size), Image.LANCZOS)
            rows.append((sq, circle(big).resize((size, size), Image.LANCZOS)))
        wm = Image.open(out / "tingxia-wordmark.png").convert("RGBA")
        sheet = Image.new("RGB", (900, 560), (236, 233, 226))
        d = ImageDraw.Draw(sheet)
        d.text((20, 18), "标记（方形 / 圆形遮罩）", fill=(70, 66, 60))
        y = 48
        for sq, ci in rows:
            sheet.paste(sq.convert("RGB"), (20, y))
            sheet.paste(ci.convert("RGB"), (300, y), ci)
            y += max(sq.height, 48) + 16
        sheet.paste(wm, (420, 60), wm)
        d.text((420, 30), "字标（横排）", fill=(70, 66, 60))
        sheet.save("/tmp/logo_preview.png")

    # ── 安卓资源 ────────────────────────────────────────────────────
    res = args.android_res
    if res is None:
        # 本文件在 <repo>/infra/web/assets/logo/ → 仓库根是 parents[4]，
        # 安卓仓在**仓库的同级目录**（两个仓并列放在工作区里）
        repo_root = Path(__file__).resolve().parents[4]
        guess = repo_root.parent / "stashbox-android" / "app" / "src" / "main" / "res"
        res = guess if guess.exists() else None
    if res is not None:
        res = Path(res)
        (res / "drawable").mkdir(parents=True, exist_ok=True)
        (res / "drawable" / "ic_launcher_foreground.xml").write_text(
            android_foreground(), encoding="utf-8"
        )
        for dpi, px in LEGACY_DENSITIES.items():
            d = res / f"mipmap-{dpi}"
            d.mkdir(parents=True, exist_ok=True)
            rsvg(mark, d / "ic_launcher.png", px)
            rsvg(mark, d / "ic_launcher_round.png", px)
        print(f"安卓资源已写入 {res}")

    # ── 后台 SPA 的 favicon（另一个仓）────────────────────────────────
    admin_public = args.admin_public
    if admin_public is None:
        repo_root = Path(__file__).resolve().parents[4]
        guess = repo_root.parent / "stashbox-admin-web" / "public"
        admin_public = guess if guess.exists() else None
    if admin_public is not None:
        admin_public = Path(admin_public)
        (admin_public / "favicon.svg").write_text(favicon, encoding="utf-8")
        (admin_public / "tingxia-mark-180.png").write_bytes(
            (out / "tingxia-mark-180.png").read_bytes()
        )
        print(f"后台 favicon 已写入 {admin_public}")

    print(f"网页资产已写入 {out}")


if __name__ == "__main__":
    main()
