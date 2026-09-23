"""Local TTS client（macOS `say` 后端，零凭证、纯本地）。

适用场景：开发 / 内网 / 无云 TTS 凭证时，用 macmini 自带的中文语音合成。
依赖：macOS 系统 `say` 命令 + ffmpeg（aiff→mp3）。

默认音色：Tingting（中文（中国大陆））。可用 TTS_LOCAL_VOICE 覆盖。
ffmpeg 路径可用 FFMPEG_BIN 覆盖（默认 /opt/homebrew/bin/ffmpeg）。
返回 mp3 bytes，对接 distill step3/step4 的「真实音频」路径（ffmpeg 拼成可播放 m4a）。
"""

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from .base import TTSClient

log = logging.getLogger(__name__)


class LocalTTSClient(TTSClient):
    """本地 TTS（macOS say + ffmpeg）。"""

    DEFAULT_VOICE = "Tingting"

    def __init__(self, voice: str = None, ffmpeg_bin: str = None):
        self.voice = voice or os.getenv("TTS_LOCAL_VOICE", self.DEFAULT_VOICE)
        self.ffmpeg_bin = ffmpeg_bin or os.getenv("FFMPEG_BIN", "/opt/homebrew/bin/ffmpeg")

    @property
    def provider_name(self) -> str:
        return "local"

    async def synthesize(
        self,
        text: str,
        voice: str = None,
        output_format: str = "mp3",
    ) -> bytes:
        if not text or not text.strip():
            raise ValueError("text 不能为空")

        chosen_voice = voice or self.voice
        log.info("LocalTTS synthesize: voice=%s text_len=%d", chosen_voice, len(text))

        # 文本落临时文件：避免 shell 引号转义 + 超长参数问题（say -f 读文件）
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
            tf.write(text)
            text_path = tf.name
        aiff_path = text_path + ".aiff"
        mp3_path = text_path + ".mp3"
        try:
            # 1) say → aiff（macOS 系统 TTS，纯本地、零凭证）
            say_proc = await asyncio.to_thread(
                subprocess.run,
                ["say", "-v", chosen_voice, "-o", aiff_path, "-f", text_path],
                capture_output=True,
                text=True,
            )
            if say_proc.returncode != 0 or not Path(aiff_path).exists():
                raise RuntimeError(f"say 合成失败 (voice={chosen_voice}): {say_proc.stderr[-500:]}")

            # 2) ffmpeg aiff → mp3（step4_concat 期望 mp3 bytes 作为输入段）
            ffmpeg_proc = await asyncio.to_thread(
                subprocess.run,
                [
                    self.ffmpeg_bin,
                    "-y",
                    "-i",
                    aiff_path,
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "96k",
                    mp3_path,
                ],
                capture_output=True,
                text=True,
            )
            if ffmpeg_proc.returncode != 0 or not Path(mp3_path).exists():
                raise RuntimeError(f"ffmpeg 转码失败: {ffmpeg_proc.stderr[-500:]}")

            return Path(mp3_path).read_bytes()
        finally:
            for p in (text_path, aiff_path, mp3_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
