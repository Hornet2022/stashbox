package com.tingxia.audio.audio

/**
 * MediaSession 回调占位
 *
 * CP4.2 仅声明 — CP4.4 后台播放 / CP4.5 锁屏控制 时才实现
 * androidx.media3.session.MediaSession.Callback
 */
object MediaSessionCallback {

    /** CP4.4 接入后置为 true */
    const val ENABLED: Boolean = false
}
