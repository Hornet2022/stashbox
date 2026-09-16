"""Mock TTS Client（CP3.5-pre-2）。

CP3.5 才接真豆包 TTS。本期 mock 返 URL 占位。
"""


class MockTTSClient:
    """豆包 TTS 占位实现：不发起任何网络请求，只返固定音频段。"""

    async def synthesize(self, text: str) -> list[dict]:
        """返回 2 段假音频段。"""
        return [
            {"text": "段1", "voice": "zh_male_gentle", "audio_url": "https://mock/seg1.m4a"},
            {"text": "段2", "voice": "zh_female_lively", "audio_url": "https://mock/seg2.m4a"},
        ]
