from __future__ import annotations

import os
import json
from pathlib import Path
import asyncio
import sys
import types

import pytest
from fastapi.testclient import TestClient

from control_plane.app import AllocateRequest, DockerSandboxBackend, ReleaseRequest, SandboxControl, _build_control_plane_app
from control_plane.snapshot_store import LocalSnapshotStore


def test_local_snapshot_store_saves_versions_and_restores(tmp_path: Path) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "CLAUDE.md").write_text("memory v1")

    store = LocalSnapshotStore(tmp_path / "snapshots")
    first = store.save("user_001", [source])
    assert first.version == 1
    assert first.file_count == 1

    (source / "main.py").write_text("print('hello')")
    second = store.save("user_001", [source])
    assert second.version == 2
    assert second.file_count == 2
    assert store.latest("user_001") == second

    restore_root = tmp_path / "restore"
    restored = store.restore("user_001", restore_root)
    assert restored == second
    assert (restore_root / "workspace" / "CLAUDE.md").read_text() == "memory v1"
    assert (restore_root / "workspace" / "main.py").read_text() == "print('hello')"


class _FakeBackend:
    def __init__(self) -> None:
        self.allocate_count = 0
        self.released: list[str] = []

    def allocate(self, runtime, endpoint_hint):
        self.allocate_count += 1
        return "http://127.0.0.1:8765"

    def release(self, runtime, remove: bool = False) -> None:
        self.released.append(f"{runtime.sandbox_id}:{remove}")

    def heartbeat(self, runtime) -> bool:
        return False if runtime.status == "stopped" else True


class _FakeUpstreamResponse:
    def __init__(self, content: bytes, status_code: int = 200, headers: dict[str, str] | None = None) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}

    async def aread(self) -> bytes:
        return self.content

    async def aiter_bytes(self):
        yield self.content


class _FakeStreamContext:
    def __init__(self, response: _FakeUpstreamResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeUpstreamResponse:
        return self.response

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeProxyAsyncClient:
    calls: list[dict[str, object]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.calls.append({"kind": "init", "kwargs": kwargs})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def request(self, method: str, url: str, *args: object, **kwargs: object) -> _FakeUpstreamResponse:
        del args
        payload = {"method": method, "url": url, "body": (kwargs.get("content") or b"").decode()}
        self.calls.append({"kind": "request", **payload})
        return _FakeUpstreamResponse(json.dumps(payload).encode())

    def stream(self, method: str, url: str, *args: object, **kwargs: object) -> _FakeStreamContext:
        del args, kwargs
        self.calls.append({"kind": "stream", "method": method, "url": url})
        return _FakeStreamContext(_FakeUpstreamResponse(b"event: result\ndata: {}\n\n", headers={"content-type": "text/event-stream"}))


def test_control_plane_enforces_standard_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def run() -> None:
        monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
        monkeypatch.setenv("SANDBOX_STATE_ROOT", str(tmp_path / "sandboxes"))
        monkeypatch.setenv("SANDBOX_POOL_SIZE", "0")

        control = SandboxControl()
        fake_backend = _FakeBackend()
        control.backend = fake_backend

        sandbox = await control.get_or_allocate(AllocateRequest(userId="u1"))
        assert fake_backend.allocate_count == 1

        user_root = sandbox.restore_root
        (user_root / "workspace").mkdir(parents=True, exist_ok=True)
        (user_root / "workspace" / "a.txt").write_text("x")
        (user_root / ".claude").mkdir(parents=True, exist_ok=True)
        (user_root / ".claude" / "b.txt").write_text("y")
        outside = tmp_path / "outside"
        outside.mkdir()
        outside.joinpath("bad.txt").write_text("z")

        payload, _, _ = await control.release(
            ReleaseRequest(userId="u1", snapshotPaths=[str(outside / "bad.txt")])
        )
        assert payload["sandbox_id"] == sandbox.sandbox_id
        assert payload["status"] == "released"
        assert len(payload["snapshot"]["changed_files"]) >= 2

    asyncio.run(run())


def test_control_plane_user_release_request_model() -> None:
    req = ReleaseRequest(userId="u1", snapshotPaths=["/tmp/a", "/tmp/.claude"])
    assert req.user_id == "u1"
    assert req.snapshot_paths == ["/tmp/a", "/tmp/.claude"]


def test_control_plane_user_v1_proxy_covers_configuration_routes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import control_plane.app as cp

    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    monkeypatch.setenv("SANDBOX_STATE_ROOT", str(tmp_path / "sandboxes"))
    monkeypatch.setenv("SANDBOX_POOL_SIZE", "0")
    monkeypatch.setattr(cp.httpx, "AsyncClient", _FakeProxyAsyncClient)
    monkeypatch.setattr(DockerSandboxBackend, "allocate", lambda self, runtime, endpoint_hint: "http://sandbox.local:8765")
    monkeypatch.setattr(DockerSandboxBackend, "heartbeat", lambda self, runtime: True)

    _FakeProxyAsyncClient.calls = []
    app = _build_control_plane_app()
    with TestClient(app) as client:
        response = client.post("/users/u1/v1/hooks/validate?trace=1", json={"hooks": {}})

    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "POST"
    assert body["url"] == "http://sandbox.local:8765/v1/hooks/validate?trace=1"
    assert any(call["kind"] == "init" and call["kwargs"].get("trust_env") is False for call in _FakeProxyAsyncClient.calls)


def test_control_plane_user_v1_proxy_streams_session_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import control_plane.app as cp

    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    monkeypatch.setenv("SANDBOX_STATE_ROOT", str(tmp_path / "sandboxes"))
    monkeypatch.setenv("SANDBOX_POOL_SIZE", "0")
    monkeypatch.setattr(cp.httpx, "AsyncClient", _FakeProxyAsyncClient)
    monkeypatch.setattr(DockerSandboxBackend, "allocate", lambda self, runtime, endpoint_hint: "http://sandbox.local:8765")
    monkeypatch.setattr(DockerSandboxBackend, "heartbeat", lambda self, runtime: True)

    _FakeProxyAsyncClient.calls = []
    app = _build_control_plane_app()
    with TestClient(app) as client:
        with client.stream("GET", "/users/u1/v1/sessions/s1/events") as response:
            payload = response.read().decode()

    assert response.status_code == 200
    assert "event: result" in payload
    assert any(
        call["kind"] == "stream"
        and call["url"] == "http://sandbox.local:8765/v1/sessions/s1/events"
        for call in _FakeProxyAsyncClient.calls
    )


def test_control_plane_persists_sandbox_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    monkeypatch.setenv("SANDBOX_STATE_ROOT", str(tmp_path / "sandboxes"))
    monkeypatch.setenv("SANDBOX_POOL_SIZE", "0")

    async def run() -> None:
        control = SandboxControl()
        control.backend = _FakeBackend()
        await control.get_or_allocate(AllocateRequest(userId="u1"))
        assert "u1" in control._assignments
        assert (tmp_path / "sandboxes" / "control_state.json").exists()

        restored = SandboxControl()
        assert "u1" in restored._assignments


def test_snapshot_store_incremental_restore_applies_deletions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    user_workspace = workspace
    user_workspace.mkdir(parents=True)

    (user_workspace / "a.txt").write_text("v1")
    store = LocalSnapshotStore(tmp_path / "snapshots")

    first = store.save("u2", [str(user_workspace)])
    assert first.version == 1
    assert first.is_full is True

    (user_workspace / "a.txt").unlink()
    (user_workspace / "b.txt").write_text("v2")

    second = store.save("u2", [str(user_workspace)])
    assert second.version == 2
    assert second.is_full is False
    assert "workspace/a.txt" in second.deleted_files
    assert "workspace/b.txt" in second.changed_files

    restore_root = tmp_path / "restore"
    restore_root.mkdir()
    (restore_root / "stale.txt").write_text("stale")

    restored = store.restore("u2", restore_root)
    assert restored == second
    assert not (restore_root / "stale.txt").exists()
    assert not (restore_root / "workspace" / "a.txt").exists()
    assert (restore_root / "workspace" / "b.txt").read_text() == "v2"


def test_snapshot_store_can_use_oss_like_s3_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeS3Body:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

    class FakeBoto3Client:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def head_object(self, Bucket: str, Key: str) -> None:
            if f"{Bucket}/{Key}" not in self.objects:
                raise Exception("NotFound")

        def download_file(self, bucket: str, key: str, target: str) -> None:
            payload = self.objects.get(f"{bucket}/{key}")
            if payload is None:
                raise Exception("NotFound")
            Path(target).write_bytes(payload)

        def upload_file(self, source: str, bucket: str, key: str) -> None:
            self.objects[f"{bucket}/{key}"] = Path(source).read_bytes()

        def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:
            self.objects[f"{Bucket}/{Key}"] = Body

        def delete_object(self, Bucket: str, Key: str) -> None:
            self.objects.pop(f"{Bucket}/{Key}", None)

        def get_object(self, Bucket: str, Key: str) -> dict[str, FakeS3Body]:
            payload = self.objects.get(f"{Bucket}/{Key}")
            if payload is None:
                raise Exception("NotFound")
            return {"Body": FakeS3Body(payload)}

    fake_client = FakeBoto3Client()

    class FakeBoto3Module(types.SimpleNamespace):
        def client(self, *args, **kwargs):  # type: ignore[override]
            return fake_client

    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module())
    monkeypatch.setenv("SNAPSHOT_BACKEND_URI", "oss://acme-bucket/control-plane/snapshots")
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "secret")
    monkeypatch.setenv("OSS_REGION", "cn-beijing")

    work = tmp_path / "workspace"
    work.mkdir()
    (work / "hello.txt").write_text("x")

    store = LocalSnapshotStore(tmp_path / "unused", os.getenv("SNAPSHOT_BACKEND_URI"))
    first = store.save("uoss", [str(work)])
    assert first.version == 1

    restore_root = tmp_path / "restore"
    restored = LocalSnapshotStore(tmp_path / "unused2", os.getenv("SNAPSHOT_BACKEND_URI")).restore(
        "uoss",
        restore_root,
    )

    assert restored is not None
    assert restored.version == 1
    assert restored.storage_key.startswith("control-plane/snapshots/uoss/snapshots/full/")
    assert (restore_root / "workspace" / "hello.txt").read_text() == "x"


def test_docker_port_parser_tolerates_noisy_host_port():
    backend = DockerSandboxBackend()
    noisy = '{"8765/tcp":[{"HostIp":"0.0.0.0","HostPort":"55000]"}],"80/tcp":[]}'
    assert backend._extract_host_port_from_json(noisy, "8765") == "55000"


def test_snapshot_store_compacts_and_prunes_old_versions(tmp_path: Path) -> None:
    root = tmp_path / "user-root"
    workspace = root / "workspace"
    claude_dir = root / ".claude"
    workspace.mkdir(parents=True)
    claude_dir.mkdir(parents=True)

    store = LocalSnapshotStore(tmp_path / "snapshots")
    (workspace / "a.txt").write_text("v1")
    first = store.save("u-prune", base_root=root)

    (workspace / "b.txt").write_text("v2")
    second = store.save("u-prune", base_root=root)

    (claude_dir / "settings.json").write_text("{}")
    third = store.save("u-prune", base_root=root)
    compacted = store.compact("u-prune")
    assert compacted is not None
    assert compacted.version == 4
    assert compacted.is_full is True
    assert compacted.base_version is None

    removed = store.prune("u-prune", keep_last=1)
    assert [record.version for record in removed] == [first.version, second.version, third.version]
    assert [record.version for record in store._records("u-prune")] == [compacted.version]

    restore_root = tmp_path / "restore"
    restored = store.restore("u-prune", restore_root)
    assert restored == compacted
    assert (restore_root / "workspace" / "a.txt").read_text() == "v1"
    assert (restore_root / "workspace" / "b.txt").read_text() == "v2"
    assert (restore_root / ".claude" / "settings.json").read_text() == "{}"


def test_control_plane_snapshot_compact_and_prune_routes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "snapshots"
    state_root = tmp_path / "sandboxes"
    source_root = tmp_path / "source"
    (source_root / "workspace").mkdir(parents=True)
    (source_root / ".claude").mkdir(parents=True)

    store = LocalSnapshotStore(snapshot_root)
    (source_root / "workspace" / "a.txt").write_text("v1")
    store.save("u-route", base_root=source_root)
    (source_root / "workspace" / "b.txt").write_text("v2")
    store.save("u-route", base_root=source_root)

    monkeypatch.setenv("SNAPSHOT_ROOT", str(snapshot_root))
    monkeypatch.setenv("SANDBOX_STATE_ROOT", str(state_root))
    monkeypatch.setenv("SANDBOX_POOL_SIZE", "0")

    app = _build_control_plane_app()
    with TestClient(app) as client:
        compact = client.post("/control/snapshots/u-route/compact")
        assert compact.status_code == 200
        assert compact.json()["is_full"] is True

        pruned = client.post("/control/snapshots/u-route/prune", json={"keepLast": 1})
        assert pruned.status_code == 200
        assert pruned.json()["removed_versions"] == [1, 2]
