"""nginx 路由改版：把「公开面」和「管理/内测面」分开。

## 改完的路由

    stashbox.cn/            → 营销页（品牌主站）
    www.stashbox.cn/        → 营销页
    stashbox.cn/api/*       → 网关（App 的 base URL 就是这个 host）
    stashbox.cn/audio/*     → 网关（音频 URL 由服务端下发）
    stashbox.cn/downloads/* → APK
    admin.stashbox.cn/      → 后台 SPA（/login 登录，未登录自动跳）
    8.148.9.151/            → 后台 SPA（IP 回退路径，**保持不变**）

## 为什么 apex 要同时是营销页和 API

App 的 base URL 是「host + /api/v1/...」，音频 URL 由服务端下发
（articles.audio_url）—— 两者都得能从同一个 host 走通，所以用**路径分流**
而不是再开一个子域名。

## 为什么后台单独一个域名

后台的登录态存在 localStorage（AdminGuard → /login）。
公开页和后台**不同源**，公开页上的任何 XSS 都碰不到后台的 token。
"""

SHARED = """    # ── TLS（域名证书，SAN: stashbox.cn / www.stashbox.cn）────────────
    ssl_certificate     /etc/letsencrypt/live/stashbox.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/stashbox.cn/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    # ── 安全响应头 ────────────────────────────────────────────────
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    client_max_body_size 10m;
    client_body_timeout 30s;

    # ── 转发到网关（头写在 server 层，location 层继承）────────────
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Port  $server_port;
"""

NEW_BLOCK = """
# ══════════════════════════════════════════════════════════════════
# 公开面：品牌主站（stashbox.cn / www.stashbox.cn）
#   /            → 营销页（infra/web/index.html 构建产物）
#   /api/*       → 网关（App 的 base URL 就是这个 host）
#   /audio/*     → 网关（音频 URL 由服务端 articles.audio_url 下发）
#   /downloads/* → APK
#
# ⚠️ 营销页与后台**不同源**（后台在 admin.stashbox.cn）——
#    后台的登录态在 localStorage，同源就等于把后台的 token 暴露给公开页。
# ══════════════════════════════════════════════════════════════════
server {
    listen 80;
    listen [::]:80;
    server_name stashbox.cn www.stashbox.cn;

    # ACME 验证路径（http-01）——证书续签要能过
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
        default_type "text/plain";
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name stashbox.cn www.stashbox.cn;

%(shared)s
    root /var/www/stashbox-site;
    index index.html;

    # 登录端点有独立限流（防撞库）
    location = /api/v1/auth/token {
        limit_req zone=stashbox_auth burst=60 nodelay;
        proxy_pass http://stashbox_gateway;
    }

    location /api/ {
        proxy_pass http://stashbox_gateway;
    }

    # 音频：nginx 的 root 下没有这个目录，必须显式转给网关，
    # 否则客户端拿到 404（表现为「文章有音频但播不了」）
    location /audio/ {
        proxy_pass http://stashbox_gateway;
    }

    # APK 下载（共享片段：漏了会返回 index.html 而不是 APK，状态码还是 200）
    include /etc/nginx/snippets/stashbox-downloads.conf;

    # 营销页是单页，路径回落到 index.html
    location / {
        try_files $uri $uri/ /index.html;
    }

    # 营销页改动要能立刻生效 → 不缓存 html
    location = /index.html {
        add_header Cache-Control "no-cache, no-store, must-revalidate";
    }

    access_log /var/log/nginx/stashbox-site.access.log;
    error_log  /var/log/nginx/stashbox-site.error.log warn;
}
"""


def main() -> int:
    path = "/etc/nginx/sites-enabled/stashbox"
    with open(path, encoding="utf-8") as f:
        src = f.read()

    # ① apex 从 API 块拿掉（它现在是营销页）
    src = src.replace(
        "server_name api.stashbox.cn stashbox.cn;",
        "server_name api.stashbox.cn;",
    )
    # ② www 从后台块拿掉
    src = src.replace(
        "server_name admin.stashbox.cn www.stashbox.cn;",
        "server_name admin.stashbox.cn;",
    )
    # ③ 插入营销块（放在 API 块之前）
    marker = "server {\n    listen 443 ssl http2;\n    listen [::]:443 ssl http2;\n    server_name api.stashbox.cn;"
    if marker not in src:
        raise SystemExit("没找到 API 块的插入锚点")
    src = src.replace(marker, (NEW_BLOCK % {"shared": SHARED}) + "\n" + marker, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("nginx 配置已改")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
