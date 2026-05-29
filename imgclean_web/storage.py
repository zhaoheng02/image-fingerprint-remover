"""Storage backends for cleaned image artifacts."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True)
class StoredObject:
    download_url: str
    local_path: Path | None = None


class LocalStorage:
    def __init__(self, data_dir: Path, ttl_seconds: int):
        self.data_dir = data_dir
        self.ttl_seconds = ttl_seconds

    def put_cleaned(self, job_id: str, filename: str, content: bytes, suffix: str) -> StoredObject:
        job_dir = self.data_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        output_path = job_dir / f"output{suffix}"
        output_path.write_bytes(content)
        (job_dir / "job.txt").write_text(f"{filename}\n{output_path.name}\n", encoding="utf-8")
        return StoredObject(download_url=f"/download/{job_id}", local_path=output_path)

    def put_original(self, job_id: str, filename: str, content: bytes, suffix: str) -> None:
        job_dir = self.data_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / f"input{suffix}").write_bytes(content)

    def purge_expired(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        for path in self.data_dir.iterdir():
            if not path.is_dir():
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                for child in path.iterdir():
                    child.unlink(missing_ok=True)
                path.rmdir()
            except OSError:
                continue


class R2Storage:
    def __init__(
        self,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        ttl_seconds: int,
    ):
        import boto3

        self.bucket = bucket
        self.ttl_seconds = ttl_seconds
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    def put_original(self, job_id: str, filename: str, content: bytes, suffix: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=f"uploads/{job_id}/input{suffix}",
            Body=content,
            ContentType=_content_type(suffix),
            Metadata={"filename": filename},
        )

    def put_cleaned(self, job_id: str, filename: str, content: bytes, suffix: str) -> StoredObject:
        key = f"cleaned/{job_id}/output{suffix}"
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=_content_type(suffix),
            ContentDisposition=f'attachment; filename="{filename}"',
            Metadata={"filename": filename},
        )
        url = self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.ttl_seconds,
        )
        return StoredObject(download_url=url, local_path=None)


class SupabaseStorage:
    def __init__(
        self,
        supabase_url: str,
        service_key: str,
        bucket: str,
        ttl_seconds: int,
    ):
        self.supabase_url = supabase_url.rstrip("/")
        self.service_key = service_key
        self.bucket = bucket
        self.ttl_seconds = ttl_seconds

    def put_original(self, job_id: str, filename: str, content: bytes, suffix: str) -> None:
        self._upload(f"uploads/{job_id}/input{suffix}", filename, content, suffix)

    def put_cleaned(self, job_id: str, filename: str, content: bytes, suffix: str) -> StoredObject:
        path = f"cleaned/{job_id}/output{suffix}"
        self._upload(path, filename, content, suffix)
        return StoredObject(download_url=self._signed_url(path), local_path=None)

    def _upload(self, path: str, filename: str, content: bytes, suffix: str) -> None:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{self.supabase_url}/storage/v1/object/{self.bucket}/{path}",
                content=content,
                headers={
                    **self._headers(),
                    "content-type": _content_type(suffix),
                    "content-disposition": f'attachment; filename="{filename}"',
                    "x-upsert": "true",
                },
            )
        response.raise_for_status()

    def _signed_url(self, path: str) -> str:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.supabase_url}/storage/v1/object/sign/{self.bucket}/{path}",
                json={"expiresIn": self.ttl_seconds},
                headers=self._headers(),
            )
        response.raise_for_status()
        signed_url = response.json()["signedURL"]
        if signed_url.startswith("http"):
            return signed_url
        return f"{self.supabase_url}/storage/v1{signed_url}"

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "authorization": f"Bearer {self.service_key}",
        }


def _content_type(suffix: str) -> str:
    return "image/jpeg" if suffix.lower() in (".jpg", ".jpeg") else "image/png"
