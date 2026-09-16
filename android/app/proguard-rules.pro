# Add project specific ProGuard rules here.
# You can control the set of applied configuration files using the
# proguardFiles setting in build.gradle.
#
# For more details, see
#   http://developer.android.com/guide/developing/tools/proguard.html

# Uncomment this to preserve the line number information for
# debugging stack traces.
#-keepattributes SourceFile,LineNumberTable

# Media3 / ExoPlayer — CP4.4 启用混淆时按需补充
#-keep class androidx.media3.** { *; }

# Retrofit / kotlinx.serialization — CP4.6 启用混淆时按需补充
#-keepattributes Signature
#-keep class com.tingxia.audio.data.remote.** { *; }
