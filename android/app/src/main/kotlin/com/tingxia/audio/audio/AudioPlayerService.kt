package com.tingxia.audio.audio

import android.app.Service
import android.content.Intent
import android.os.IBinder

/**
 * 音频播放器前台服务骨架
 * CP4.2 仅占位 — CP4.4 接 MediaSession + ExoPlayer
 */
class AudioPlayerService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null
}
