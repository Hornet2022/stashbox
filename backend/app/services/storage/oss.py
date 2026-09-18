"""阿里云 OSS 存储（生产用，留接口）。"""

from .base import Storage


class OSSStorage(Storage):
    """阿里云 OSS 存储（接口预留，未实现）。

    切换到真 OSS 时：
    - pip install oss2
    - 实现 save() 用 oss2.Bucket.put_object
    """

    def __init__(
        self,
        access_key_id: str = "",
        access_key_secret: str = "",
        endpoint: str = "",
        bucket_name: str = "",
    ):
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.endpoint = endpoint
        self.bucket_name = bucket_name

    async def save(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        raise NotImplementedError("OSSStorage 待实现。pip install oss2 + 实现 save()。")

    async def exists(self, key: str) -> bool:
        raise NotImplementedError("OSSStorage 待实现。")
