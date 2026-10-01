"""webrtcvad 占位模块（本地 TTS 栈专用）。

`mlx_audio[server]` 依赖 webrtcvad，但它是一个需要本地编译 C 扩展的包，
在无 Xcode CLT 的机器上装不上（`Failed building wheel for webrtcvad`）。

它只在 `mlx_audio.server` 的实时语音转写路由
（`/v1/audio/transcriptions/realtime`，server.py:608）里用到，与 TTS
（`/v1/audio/speech`）完全无关。oMLX.app 自带的 site-packages 里也有
`webrtcvad.py`，但那个文件 `import pkg_resources`，而 oMLX 打包的
setuptools 已经不再提供 pkg_resources，同样不可用。

所以这里放一个 import 期可用的最小占位实现：让 server 模块能正常加载，
一旦真有人调实时转写路由就立刻显式报错，而不是等到运行时才崩。
"""


class Vad:
    """占位：真实 webrtcvad.Vad 的构造在此显式失败。"""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "webrtcvad 未安装：仅实时语音转写路由 "
            "(/v1/audio/transcriptions/realtime) 需要它，TTS 不受影响。"
        )
