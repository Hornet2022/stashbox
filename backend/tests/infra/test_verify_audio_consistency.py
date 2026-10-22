"""生产事故回归：音频一致性核对器的判据与联锁。

背景（2026-10-22 生产事故）：
    「清理孤儿音频」时，因为参考集不完整 + 没有联锁，误删了 6 篇仍在用的文章音频，
    用户点播放直接 404。事后才补齐判据并把文件还原。

这里守的是三条不变量：
  1. **键归一化唯一** —— URL / oss_key / 文件名三种形态必须归到同一个文件名，
     否则「引用集合」和「磁盘集合」根本不可比（不可比 → 必然误判）。
  2. **联锁** —— 只要存在「DB 引用但磁盘没有」，就禁止任何删除动作。
     参考集不可信时，「这个文件没人引用」同样不可信。
  3. **只移不删** —— 清理动作是「移到隔离目录」，不是 rm/unlink。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location(
    "verify_audio_consistency", SCRIPTS_DIR / "verify_audio_consistency.py"
)
verify = importlib.util.module_from_spec(_spec)
sys.modules["verify_audio_consistency"] = verify
_spec.loader.exec_module(verify)


# ── 1. 键归一化 ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expect",
    [
        # 生产真实形态：网关 URL，key 里带一层 audio/ 目录
        ("https://api.stashbox.cn/audio/audio/art_x.abc12345.mp3", "art_x.abc12345.mp3"),
        # 带 query（OSS presigned）
        ("https://oss.example.com/audio/art_x.mp3?sig=abc&exp=1", "art_x.mp3"),
        # 带 fragment
        ("https://api.stashbox.cn/audio/audio/art_x.mp3#t=10", "art_x.mp3"),
        # oss_key 形态（库里存的是带目录前缀的 key）
        ("audio/art_x.abc12345.mp3", "art_x.abc12345.mp3"),
        # 已经是文件名
        ("art_x.abc12345.mp3", "art_x.abc12345.mp3"),
        # 尾部斜杠
        ("https://api.stashbox.cn/audio/audio/art_x.mp3/", "art_x.mp3"),
        # 空
        ("", ""),
        (None, ""),
        ("   ", ""),
    ],
)
def test_normalize_key_统一形态(raw, expect):
    assert verify.normalize_key(raw) == expect


def test_normalize_key_大小写与路径分隔不敏感():
    assert verify.normalize_key("AUDIO/ART_X.MP3") == "ART_X.MP3"


# ── 2. 双向判定 ────────────────────────────────────────────────────────
def test_build_plan_区分引用缺失与无主文件():
    referenced = {
        "art_a.mp3": "articles.audio_url",
        "art_b.mp3": "distilled_articles.audio_url",
    }
    on_disk = {"art_b.mp3", "art_c.mp3"}
    plan = verify.build_plan(referenced, on_disk)
    assert plan.missing_refs == {"art_a.mp3"}  # DB 有、磁盘无
    assert plan.orphan_files == {"art_c.mp3"}  # 磁盘有、DB 无
    assert plan.ref_sources["art_b.mp3"] == "distilled_articles.audio_url"


def test_生产事故形状_有引用缺失时禁止删除():
    """6 篇在用的音频被删 + 若干无主文件 —— 当年的脚本在这个形状下删了文件。"""
    referenced = {f"art_live{i}.hash.mp3": "distilled_articles.audio_url" for i in range(6)}
    on_disk = {f"art_orphan{i}.mp3" for i in range(3)}
    plan = verify.build_plan(referenced, on_disk)
    assert plan.can_delete is False
    reason = plan.block_reason()
    assert "引用缺失" in reason or "引用" in reason
    assert "禁止删除" in reason


def test_没有引用缺失时才允许清理():
    referenced = {"art_a.mp3": "articles.audio_url"}
    on_disk = {"art_a.mp3", "art_orphan.mp3"}
    plan = verify.build_plan(referenced, on_disk)
    assert plan.can_delete is True
    assert plan.block_reason() == ""
    assert plan.orphan_files == {"art_orphan.mp3"}


# ── 3. 参考集列名不能漏 ────────────────────────────────────────────────
def test_参考集列名覆盖三类音频引用列():
    """少一个列名 = 少一类引用 = 那类文件会被当成「没人引用」而删掉。"""
    assert set(verify.AUDIO_REF_COLUMNS) >= {"audio_url", "oss_key", "audio_path"}


def test_脚本不硬编码表名_参考集靠information_schema自动发现():
    """表名硬编码会漂移：新加一张存音频的表而忘了加进列表 → 误删。"""
    src = (SCRIPTS_DIR / "verify_audio_consistency.py").read_text(encoding="utf-8")
    assert "information_schema" in src
    for table in ("distilled_articles", "article_audio_variants"):
        assert f'"{table}"' not in src, f"脚本里硬编码了表名 {table}，参考集必须自动发现"


# ── 4. 只移不删 ────────────────────────────────────────────────────────
def test_清理动作只移不删():
    src = (SCRIPTS_DIR / "verify_audio_consistency.py").read_text(encoding="utf-8")
    for forbidden in ("os.remove", ".unlink(", "rmtree"):
        assert forbidden not in src, f"脚本出现了真删除动作：{forbidden}"
    assert "shutil.move" in src


# ── 5. CLI 默认只读 + 联锁在 CLI 层也生效 ───────────────────────────────
def test_cli_默认只读不动文件(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "art_live.mp3").write_bytes(b"x")
    (audio_dir / "art_orphan.mp3").write_bytes(b"y")

    async def fake_refs():
        return {"art_live.mp3": "articles.audio_url"}

    monkeypatch.setattr(verify, "collect_referenced", fake_refs)
    monkeypatch.setattr(verify, "collect_on_disk", lambda d: {"art_live.mp3", "art_orphan.mp3"})

    rc = verify.main(["--audio-dir", str(audio_dir)])
    assert rc == 0  # 没有引用缺失
    assert (audio_dir / "art_orphan.mp3").exists(), "默认不该动文件"


def test_cli_有引用缺失时拒绝清理(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "art_orphan.mp3").write_bytes(b"y")

    async def fake_refs():
        return {"art_missing.mp3": "articles.audio_url"}

    monkeypatch.setattr(verify, "collect_referenced", fake_refs)
    monkeypatch.setattr(verify, "collect_on_disk", lambda d: {"art_orphan.mp3"})

    rc = verify.main(["--audio-dir", str(audio_dir), "--delete-orphans", "--yes"])
    assert rc == 2, "有引用缺失时必须拒绝清理"
    assert (audio_dir / "art_orphan.mp3").exists(), "被拒绝时不能动文件"
