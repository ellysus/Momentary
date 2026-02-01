import io
from dataclasses import dataclass
from typing import Optional

from minio import Minio
from minio.error import S3Error


@dataclass
class MinioConfig:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    secure: bool = False


class MinioStorage:
    def __init__(self, config: MinioConfig) -> None:
        self._client = Minio(
            config.endpoint,
            access_key=config.access_key,
            secret_key=config.secret_key,
            secure=config.secure,
        )
        self._bucket = config.bucket

    def ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    def upload_photo(
        self, object_key: str, data: bytes, content_type: Optional[str]
    ) -> None:
        self.ensure_bucket()
        self._client.put_object(
            self._bucket,
            object_key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "image/jpeg",
        )

    def get_presigned_url(self, object_key: str) -> Optional[str]:
        try:
            return self._client.presigned_get_object(self._bucket, object_key)
        except S3Error:
            return None
