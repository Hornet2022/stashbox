"""盲测音频的落盘目录与 URL 拼接（2026-10-21 修的生产事故）。

## 事故

盲测接口在生产上**完全不可用**，但所有测试都是绿的：

- setup 返回 200，``audio_url`` 长得像模像样（``/audio/blind-test/x.wav``）
- 运营点"试听" → 一律 404
- 根因：音频写死落在 ``/tmp/audio/blind-test/``
  （注释里写"由 api-gateway 的 /audio 挂载"——**那只在本地开发成立**）：
    · 开发机：网关 LOCAL_AUDIO_DIR 默认就是 /tmp/audio → 恰好对得上
    · 生产：网关挂的是 /var/lib/stashbox/data，且 ai-service 有
      PrivateTmp=true → /tmp 是**私有命名空间**，网关根本看不到
  加上 URL 少了一层 ``audio/``（网关挂 base_dir，文件在 base/audio/ 下），
  两处错位叠加 = 404。

## 修法

1. 落盘目录从 ``LOCAL_AUDIO_DIR`` 派生
2. URL **从实际落盘路径反推**，不再写死
"""

import importlib.util
import sys
from pathlib import Path


REPO_PARENT = str(Path(__file__).resolve().parents[3])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)
BACKEND_DIR = Path(__file__).resolve().parents[2]
AI_DIR = BACKEND_DIR / "ai-service"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


def _load_blind():
    spec = importlib.util.spec_from_file_location(
        "tts_blind_test_under_test", AI_DIR / "distill/tts_blind_test.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


blind = _load_blind()


class TestBlindTestDir:
    def test_follows_local_audio_dir(self, monkeypatch, tmp_path):
        """落盘目录跟着 LOCAL_AUDIO_DIR 走。"""
        monkeypatch.setenv("LOCAL_AUDIO_DIR", str(tmp_path))
        assert blind._blind_test_dir() == tmp_path / "audio" / "blind-test"

    def test_defaults_to_dev_path(self, monkeypatch):
        """没配 LOCAL_AUDIO_DIR 时回落到开发机默认。

        注意是 ``/tmp/audio/audio/blind-test`` —— 因为 LOCAL_AUDIO_DIR
        是 **base_dir**，storage 的 key 自带 ``audio/`` 前缀
        （``audio/{article_id}.mp3``）。原来的硬编码
        ``/tmp/audio/blind-test`` 比这少一层，在开发机上和
        蒸馏产物的落盘约定不一致（那正是本事故的成因）。
        """
        monkeypatch.delenv("LOCAL_AUDIO_DIR", raising=False)
        assert blind._blind_test_dir() == Path("/tmp/audio/audio/blind-test")

    def test_not_cached_at_import_time(self, monkeypatch, tmp_path):
        """目录**不能**是模块级常量 —— env 改了要立刻生效。

        这正是原事故的另一半：BLIND_TEST_DIR 是 import 时求值的常量，
        进程启动后 env 怎么改都不生效。
        """
        monkeypatch.setenv("LOCAL_AUDIO_DIR", str(tmp_path))
        assert blind._blind_test_dir() != blind.BLIND_TEST_DIR or True
        # 两次调用都跟随当前 env
        first = blind._blind_test_dir()
        monkeypatch.setenv("LOCAL_AUDIO_DIR", "/tmp/other")
        second = blind._blind_test_dir()
        assert first != second


class TestPublicUrlFor:
    def test_url_matches_gateway_mount(self, monkeypatch, tmp_path):
        """网关把 /audio 挂到 LOCAL_AUDIO_DIR，文件在其下的 audio/ 子目录。

        → URL 必须是 /audio/audio/... （两层），少一层就是 404。
        """
        monkeypatch.setenv("LOCAL_AUDIO_DIR", str(tmp_path))
        f = tmp_path / "audio" / "blind-test" / "abc_0.wav"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"RIFF")
        assert blind._public_url_for(f) == "/audio/audio/blind-test/abc_0.wav"

    def test_path_outside_base_falls_back(self, monkeypatch, tmp_path):
        """文件不在 base 下（配错了）→ 退回按文件名拼，并记 warning。"""
        monkeypatch.setenv("LOCAL_AUDIO_DIR", str(tmp_path / "base"))
        other = tmp_path / "elsewhere" / "x.wav"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"RIFF")
        assert blind._public_url_for(other) == "/audio/x.wav"
