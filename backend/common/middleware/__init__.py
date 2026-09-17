"""Common middleware package（CP3.6-A2：消除 file/package 同名碰撞）。

原 backend/common/middleware.py 与 backend/common/middleware/（audit.py 所在包）
同名，import 解析到本 __init__ 而找不到 RequestIDMiddleware。现统一 re-export，
各服务 `from stashbox.backend.common.middleware import RequestIDMiddleware` 照常可用。
"""
from stashbox.backend.common.middleware.audit import AuditMiddleware
from stashbox.backend.common.middleware.request_id import RequestIDMiddleware

__all__ = ["AuditMiddleware", "RequestIDMiddleware"]
