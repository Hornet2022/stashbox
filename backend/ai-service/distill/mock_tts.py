"""Mock TTS Client（CP3.5-pre-2）。

CP3.5 才接真豆包 TTS。本期 mock 生成真实可播的静音 WAV bytes，让 pipeline._save_audio
走 LocalStorage 落盘到 /tmp/audio/audio/{article_id}.wav，ExoPlayer 直接播。

生成 30 秒 8kHz mono 16-bit PCM WAV（标准 RIFF header，~480KB），ExoPlayer 原生解码。
"""


def _make_silent_wav(duration_sec: int = 30, sample_rate: int = 8000) -> bytes:
    """生成静音 WAV（带完整 RIFF header，ExoPlayer / PCM decoder 都能直接播）。"""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        # silence = zeros
        w.writeframes(b"\x00\x00" * duration_sec * sample_rate)
    return buf.getvalue()


class MockTTSClient:
    """Mock TTS 占位实现：不发起任何网络请求，生成真可播静音 WAV bytes。

    audio_url=None 让 pipeline._save_audio 走 LocalStorage 落盘（而不是用 URL 占位）。
    返回 segment 含 "bytes" 字段，让 step4_concat 的简化分支直接命中（无 ffmpeg 路径）。
    """

    async def synthesize(self, text: str) -> list[dict]:
        """返回 1 段真 WAV bytes，audio_url=None 让 _save_audio 决定怎么存。"""
        wav_bytes = _make_silent_wav()
        return [
            {
                "text": text[:200] or "mock-tts",
                "voice": "zh_female_sophie",
                "audio_url": None,  # 让 _save_audio 上传到 LocalStorage
                "bytes": wav_bytes,  # 字段名必须是 "bytes" 才能被 step4 real_bytes 列表解析拿到
                "duration_sec": 30,
            },
        ]
