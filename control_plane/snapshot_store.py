from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from typing import Iterable


SNAPSHOT_DELETE_MARKER = ".snapshot-deletes.txt"


@dataclass(frozen=True)
class SnapshotRecord:
    user_id: str
    version: int
    storage_key: str
    file_count: int
    total_size: int
    created_at: float
    changed_file_count: int = 0
    changed_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    base_version: int | None = None
    is_full: bool = True


class _StorageBackend:
    def get(self, key: str, target: Path) -> None:
        raise NotImplementedError

    def put(self, source: Path, key: str) -> str:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def read_text(self, key: str) -> str:
        raise NotImplementedError

    def write_text(self, key: str, content: str) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class _LocalStorage(_StorageBackend):
    def __init__(self, base: Path) -> None:
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.base / key

    def get(self, key: str, target: Path) -> None:
        source = self._path(key)
        if not source.exists():
            raise FileNotFoundError(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def put(self, source: Path, key: str) -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return str(target)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def read_text(self, key: str) -> str:
        return self._path(key).read_text(encoding="utf-8")

    def write_text(self, key: str, content: str) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class _S3Storage(_StorageBackend):
    def __init__(self, uri: str) -> None:
        parsed = urlparse(uri)
        self.bucket = parsed.netloc
        self.prefix = parsed.path.strip("/")
        self.region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or os.getenv("OSS_REGION") or "us-east-1"
        self.endpoint_url = os.getenv("SNAPSHOT_S3_ENDPOINT_URL") or os.getenv("SNAPSHOT_OSS_ENDPOINT_URL") or os.getenv("OSS_ENDPOINT") or os.getenv("OSS_ENDPOINT_URL", "")

        import boto3

        client_kwargs: dict[str, str] = {"region_name": self.region}
        if self.endpoint_url:
            client_kwargs["endpoint_url"] = self.endpoint_url

        access_key = (
            os.getenv("SNAPSHOT_AWS_ACCESS_KEY_ID")
            or os.getenv("AWS_ACCESS_KEY_ID")
            or os.getenv("OSS_ACCESS_KEY_ID")
        )
        secret_key = (
            os.getenv("SNAPSHOT_AWS_SECRET_ACCESS_KEY")
            or os.getenv("AWS_SECRET_ACCESS_KEY")
            or os.getenv("OSS_ACCESS_KEY_SECRET")
            or os.getenv("OSS_SECRET_ACCESS_KEY")
        )
        session_token = (
            os.getenv("AWS_SESSION_TOKEN")
            or os.getenv("SNAPSHOT_AWS_SESSION_TOKEN")
            or os.getenv("OSS_SESSION_TOKEN")
        )
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key
            if session_token:
                client_kwargs["aws_session_token"] = session_token

        self._client = boto3.client("s3", **client_kwargs)

    def _full_key(self, key: str) -> str:
        if self.prefix and key.startswith(self.prefix.rstrip("/") + "/"):
            return key
        if not self.prefix:
            return key
        return f"{self.prefix.rstrip('/')}/{key}"

    def get(self, key: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, self._full_key(key), str(target))

    def put(self, source: Path, key: str) -> str:
        full_key = self._full_key(key)
        self._client.upload_file(str(source), self.bucket, full_key)
        return full_key

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._full_key(key))
            return True
        except Exception:
            return False

    def read_text(self, key: str) -> str:
        obj = self._client.get_object(Bucket=self.bucket, Key=self._full_key(key))
        return obj["Body"].read().decode("utf-8")

    def write_text(self, key: str, content: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=self._full_key(key), Body=content.encode("utf-8"))

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._full_key(key))


class LocalSnapshotStore:
    """Filesystem or S3-compatible snapshot store with strict incremental replay."""

    def __init__(self, root: str | Path, storage_uri: str | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if storage_uri is None:
            storage_uri = f"file://{self.root}"
        self.storage = self._build_storage(storage_uri)
        self.storage_uri = storage_uri

    @staticmethod
    def _parse_default_sources(base_root: str | Path | None) -> list[Path]:
        if base_root is None:
            return [Path(".") / ".claude", Path(".") / "workspace"]
        base = Path(base_root)
        return [base / ".claude", base / "workspace"]

    def latest(self, user_id: str) -> SnapshotRecord | None:
        records = self._records(user_id)
        return records[-1] if records else None

    def save(
        self,
        user_id: str,
        sources: list[str | Path] | None = None,
        base_root: str | Path | None = None,
        force_full: bool = False,
    ) -> SnapshotRecord:
        sources = self._normalize_sources(sources, base_root)
        user_dir = self.root / user_id
        user_dir.mkdir(parents=True, exist_ok=True)

        previous = self.latest(user_id)
        version = (previous.version + 1) if previous else 1
        manifest_map, path_map = self._collect_manifest(sources)
        previous_manifest = self._load_manifest_for_record(previous) if previous else {}

        changed_files = [
            item
            for item, digest in manifest_map.items()
            if previous is None or previous_manifest.get(item) != digest
        ]
        deleted_files = [
            item for item in previous_manifest if item not in manifest_map
        ] if previous else []

        is_full = previous is None or force_full
        archive_kind = "full" if is_full else "delta"
        local_archive = user_dir / f"v{version}.{archive_kind}.tar.gz"
        changed_file_count = len(manifest_map) if is_full else len(changed_files)
        file_count = self._count_files(sources)

        if is_full:
            with tarfile.open(local_archive, "w:gz") as tar:
                for source in sources:
                    path = Path(source).expanduser()
                    if not path.exists():
                        continue
                    tar.add(path, arcname=path.name)
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                staging = Path(tmpdir)
                for rel in sorted(changed_files):
                    source_file = path_map.get(rel)
                    if source_file is None or not source_file.exists():
                        continue
                    target_file = staging / rel
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_file, target_file)

                if deleted_files:
                    (staging / SNAPSHOT_DELETE_MARKER).write_text(
                        "\n".join(sorted(deleted_files)),
                        encoding="utf-8",
                    )

                with tarfile.open(local_archive, "w:gz") as tar:
                    for item in staging.rglob("*"):
                        if item.is_file():
                            tar.add(item, arcname=item.relative_to(staging))

        storage_key = self.storage.put(local_archive, self._archive_key(user_id, version, archive_kind))

        manifest_payload = {
            "user_id": user_id,
            "version": version,
            "base_version": None if is_full else previous.version,
            "is_full": is_full,
            "manifest": manifest_map,
            "changed_files": sorted(manifest_map) if is_full else changed_files,
            "deleted_files": [] if is_full else deleted_files,
            "file_count": file_count,
            "created_at": time.time(),
            "storage_key": storage_key,
            "changed_file_count": changed_file_count,
            "total_size": local_archive.stat().st_size,
        }
        manifest_file = user_dir / f"v{version}.manifest.json"
        manifest_file.write_text(json.dumps(manifest_payload, ensure_ascii=False), encoding="utf-8")
        self.storage.put(manifest_file, self._manifest_key(user_id, version, "manifest"))

        record = SnapshotRecord(
            user_id=user_id,
            version=version,
            storage_key=storage_key,
            file_count=file_count,
            total_size=local_archive.stat().st_size,
            created_at=manifest_payload["created_at"],
            changed_file_count=changed_file_count,
            changed_files=sorted(manifest_map) if is_full else changed_files,
            deleted_files=[] if is_full else deleted_files,
            base_version=None if is_full else previous.version,
            is_full=is_full,
        )
        self._append_record(record)
        return record

    def compact(self, user_id: str, version: int | None = None) -> SnapshotRecord | None:
        record = self._record_for_version(user_id, version)
        if record is None:
            return None

        with tempfile.TemporaryDirectory() as tmpdir:
            restore_root = Path(tmpdir) / "restore"
            self.restore(user_id, restore_root, record.version)
            return self.save(user_id, base_root=restore_root, force_full=True)

    def prune(self, user_id: str, keep_last: int = 1) -> list[SnapshotRecord]:
        records = self._records(user_id)
        if keep_last < 1:
            raise ValueError("keep_last must be >= 1")
        if len(records) <= keep_last:
            return []

        protected_versions: set[int] = set()
        for record in records[-keep_last:]:
            protected_versions.update(item.version for item in self._version_chain(user_id, record.version))

        removed = [record for record in records if record.version not in protected_versions]
        if not removed:
            return []

        remaining = [record for record in records if record.version in protected_versions]
        self._rewrite_records(user_id, remaining)
        for record in removed:
            self._delete_record_artifacts(record)
        return removed

    def restore(
        self,
        user_id: str,
        target: str | Path,
        version: int | None = None,
        base_root: str | Path | None = None,
    ) -> SnapshotRecord | None:
        del base_root
        target_path = Path(target)
        target_path.mkdir(parents=True, exist_ok=True)
        record = self._record_for_version(user_id, version)
        if record is None:
            return None

        chain = self._version_chain(user_id, record.version)
        if not chain:
            return None

        self._reset_target_path(target_path)
        for item in chain:
            local_archive = self._download_archive(item)
            self._apply_snapshot_archive(local_archive, target_path, item)

        return record

    def _apply_snapshot_archive(
        self,
        archive_path: Path,
        target_path: Path,
        record: SnapshotRecord,
    ) -> None:
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(target_path, filter="data")

        if not record.is_full:
            delete_manifest = target_path / SNAPSHOT_DELETE_MARKER
            if delete_manifest.exists():
                deleted = [
                    line.strip()
                    for line in delete_manifest.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                for deleted_path in deleted:
                    path_to_remove = target_path / deleted_path
                    if path_to_remove.is_dir():
                        shutil.rmtree(path_to_remove, ignore_errors=True)
                    elif path_to_remove.exists():
                        path_to_remove.unlink()
                delete_manifest.unlink()

    @staticmethod
    def _reset_target_path(target_path: Path) -> None:
        for item in target_path.rglob("*"):
            if item.is_file():
                item.unlink()
            else:
                shutil.rmtree(item, ignore_errors=True)

    def _version_chain(self, user_id: str, target_version: int) -> list[SnapshotRecord]:
        records = self._records(user_id)
        by_version = {record.version: record for record in records}
        if target_version not in by_version:
            raise ValueError("target version not found")

        chain: list[SnapshotRecord] = []
        cursor = by_version[target_version]
        while True:
            chain.append(cursor)
            if cursor.base_version is None:
                break
            cursor = by_version.get(cursor.base_version)
            if cursor is None:
                raise ValueError(f"broken snapshot chain: missing base snapshot v{chain[-1].base_version}")

        return list(reversed(chain))

    def latest_snapshot_meta(self, user_id: str) -> dict[str, object] | None:
        record = self.latest(user_id)
        if record is None:
            return None
        return record.__dict__

    def _record_for_version(self, user_id: str, version: int | None) -> SnapshotRecord | None:
        records = self._records(user_id)
        if not records:
            return None
        if version is None:
            return records[-1]
        return next((record for record in records if record.version == version), None)

    def _records(self, user_id: str) -> list[SnapshotRecord]:
        manifest = self.root / user_id / "manifest.jsonl"
        if not manifest.exists():
            remote_key = self._manifest_log_key(user_id)
            if self.storage.exists(remote_key):
                with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                    tmp_path = Path(tmp_file.name)
                    tmp_file.close()
                try:
                    self.storage.get(remote_key, tmp_path)
                    manifest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(tmp_path, manifest)
                finally:
                    tmp_path.unlink(missing_ok=True)
            else:
                return []

        entries: list[SnapshotRecord] = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            entries.append(
                SnapshotRecord(
                    user_id=payload.get("user_id", user_id),
                    version=int(payload.get("version", 0)),
                    storage_key=payload.get("storage_key", ""),
                    file_count=int(payload.get("file_count", 0)),
                    total_size=int(payload.get("total_size", 0)),
                    created_at=float(payload.get("created_at", 0.0)),
                    changed_file_count=int(payload.get("changed_file_count", 0)),
                    changed_files=payload.get("changed_files", []) if isinstance(payload.get("changed_files"), list) else [],
                    deleted_files=payload.get("deleted_files", []) if isinstance(payload.get("deleted_files"), list) else [],
                    base_version=payload.get("base_version"),
                    is_full=bool(payload.get("is_full", True)),
                )
            )

        return sorted(entries, key=lambda item: item.version)

    def _append_record(self, record: SnapshotRecord) -> None:
        manifest = self.root / record.user_id / "manifest.jsonl"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            (manifest.read_text(encoding="utf-8") if manifest.exists() else "")
            + json.dumps(record.__dict__, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        self.storage.put(manifest, self._manifest_log_key(record.user_id))

    def _rewrite_records(self, user_id: str, records: list[SnapshotRecord]) -> None:
        manifest = self.root / user_id / "manifest.jsonl"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            "".join(json.dumps(record.__dict__, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        self.storage.put(manifest, self._manifest_log_key(user_id))

    def _delete_record_artifacts(self, record: SnapshotRecord) -> None:
        self.storage.delete(record.storage_key)
        self.storage.delete(self._manifest_key(record.user_id, record.version, "manifest"))
        user_dir = self.root / record.user_id
        for pattern in (
            f"v{record.version}.full.tar.gz",
            f"v{record.version}.delta.tar.gz",
            f"v{record.version}.restore.tar.gz",
            f"v{record.version}.manifest.json",
        ):
            (user_dir / pattern).unlink(missing_ok=True)

    def _download_archive(self, record: SnapshotRecord) -> Path:
        local_path = self.root / record.user_id / f"v{record.version}.restore.tar.gz"
        if local_path.exists():
            return local_path

        if isinstance(self.storage, _LocalStorage):
            candidate = Path(record.storage_key)
            if candidate.exists():
                shutil.copy2(candidate, local_path)
            else:
                candidate = self.root / record.storage_key
                if candidate.exists():
                    shutil.copy2(candidate, local_path)
        else:
            self.storage.get(record.storage_key, local_path)

        return local_path

    def _normalize_sources(self, sources: list[str | Path] | None, base_root: str | Path | None) -> list[Path]:
        if sources:
            return [Path(item).expanduser() for item in sources]
        return self._parse_default_sources(base_root)

    def _collect_manifest(self, sources: Iterable[Path | str]) -> tuple[dict[str, str], dict[str, Path]]:
        manifest: dict[str, str] = {}
        path_map: dict[str, Path] = {}
        for source in sources:
            source_path = Path(source).expanduser()
            if not source_path.exists():
                continue
            if source_path.is_file():
                rel = source_path.name
                manifest[rel] = self._hash_file(source_path)
                path_map[rel] = source_path
            else:
                for item in source_path.rglob("*"):
                    if not item.is_file():
                        continue
                    rel = f"{source_path.name}/{item.relative_to(source_path).as_posix()}"
                    manifest[rel] = self._hash_file(item)
                    path_map[rel] = item

        return manifest, path_map

    def _load_manifest_for_record(self, record: SnapshotRecord | None) -> dict[str, str]:
        if record is None:
            return {}

        local_manifest = self.root / record.user_id / f"v{record.version}.manifest.json"
        if not local_manifest.exists():
            manifest_key = self._manifest_key(record.user_id, record.version, "manifest")
            if self.storage.exists(manifest_key):
                self.storage.get(manifest_key, local_manifest)
            else:
                return {}

        payload = json.loads(local_manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        return payload.get("manifest", {}) if isinstance(payload.get("manifest"), dict) else {}

    @staticmethod
    def _count_files(sources: Iterable[Path]) -> int:
        count = 0
        for source in sources:
            source_path = Path(source).expanduser()
            if not source_path.exists():
                continue
            if source_path.is_file():
                count += 1
            else:
                count += sum(1 for item in source_path.rglob("*") if item.is_file())
        return count

    def _manifest_key(self, user_id: str, version: int, kind: str) -> str:
        return f"{user_id}/snapshots/v{version}.{kind}.json"

    def _manifest_log_key(self, user_id: str) -> str:
        return f"{user_id}/snapshots/manifest.jsonl"

    def _archive_key(self, user_id: str, version: int, kind: str) -> str:
        return f"{user_id}/snapshots/{kind}/v{version}.{kind}.tar.gz"

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(8192):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _build_storage(storage_uri: str) -> _StorageBackend:
        parsed = urlparse(storage_uri)
        if parsed.scheme in {"", "file"}:
            base_path = Path(parsed.path) if parsed.path else Path(".")
            return _LocalStorage(base_path)
        if parsed.scheme in {"s3", "oss", "oss2"}:
            return _S3Storage(storage_uri)
        raise ValueError(f"Unsupported snapshot URI scheme: {parsed.scheme}")
