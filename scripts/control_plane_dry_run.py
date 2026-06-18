#!/usr/bin/env python
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import importlib
import tempfile
from contextlib import contextmanager
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _set_env(tmp_root: Path, backend: str) -> None:
    os.environ["SNAPSHOT_ROOT"] = str(tmp_root / "snapshots")
    os.environ["SANDBOX_STATE_ROOT"] = str(tmp_root / "state")
    os.environ["SANDBOX_BACKEND"] = backend
    os.environ["SANDBOX_POOL_SIZE"] = "0"
    os.environ["SANDBOX_REMOVE_ON_RELEASE"] = "false"
    os.environ["SANDBOX_PURGE_ON_IDLE"] = "false"
    os.environ["SANDBOX_WAIT_FOR_HEALTH"] = "false"
    os.environ["SANDBOX_DOCKER_USE_HOST_NETWORK"] = "false"


class _FakeAsyncResponse:
    def __init__(self, body: bytes, status_code: int = 200) -> None:
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode()


class _FakeStreamBody:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, *args: object, **kwargs: object) -> _FakeAsyncResponse:
        del args, kwargs
        if "/v1/query/stream" in url:
            payload = {"result": "mock stream event"}
            return _FakeAsyncResponse(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        payload = {"result": "mock query result", "events": [{"event": "result"}]}
        return _FakeAsyncResponse(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def stream(self, method: str, url: str, *args: object, **kwargs: object):
        del method, args, kwargs
        if "/v1/query/stream" in url:
            return _FakeStreamBody([
                b"event: system\n",
                b'data: {"event":"result"}\n\n',
            ])
        raise RuntimeError("unexpected stream target")


def _fake_docker_runner(commands: list[str], backend_id: str):
    def run(cmd: list[str], required: bool = False) -> str | None:
        del required
        commands.append(" ".join(cmd))
        if cmd[:2] == ["docker", "inspect"] and "-f" in cmd:
            template = cmd[cmd.index("-f") + 1]
            if template == "{{.Id}}":
                return f"{backend_id}-cp"
            if template == "{{.State.Status}}":
                return "running"
            if template == "{{.State.Running}}":
                return "true"
            if template == "{{json .NetworkSettings.Ports}}":
                return '{"8765/tcp":[{"HostIp":"127.0.0.1","HostPort":"54321"}]}'
            if template == "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}":
                return "127.0.0.1"
        if cmd[:2] == ["docker", "ps"] and cmd[2:4] == ["-q", "-f"]:
            return None
        if cmd[:2] == ["docker", "create"]:
            return f"{backend_id}-cp"
        if cmd[:2] == ["docker", "start"]:
            return f"{backend_id}-cp"
        if cmd[:2] == ["docker", "stop"]:
            return f"{backend_id}-cp"
        if cmd[:2] == ["docker", "rm"]:
            return f"{backend_id}-cp"
        if cmd[:2] == ["docker", "rm", "-f"]:
            return f"{backend_id}-cp"
        return None

    return run


def _fake_k8s_runner(commands: list[str], backend_id: str):
    def run(cmd: list[str], required: bool = False) -> str | None:
        del required
        commands.append(" ".join(cmd))
        if cmd[:3] == ["kubectl", "apply", "-f"]:
            return ""
        if cmd[:3] == ["kubectl", "label", "deployment"]:
            return ""
        if cmd[:3] == ["kubectl", "scale", "deployment"]:
            return ""
        if cmd[:3] == ["kubectl", "delete", "svc"]:
            return ""
        if cmd[:3] == ["kubectl", "delete", "deployment"]:
            return ""
        if cmd[:3] == ["kubectl", "get", "deployment"] and any(arg.startswith("jsonpath={.metadata.name}") for arg in cmd):
            return f"{backend_id}-cp"
        if cmd[:3] == ["kubectl", "get", "deployment"] and any(arg.startswith("jsonpath={.status.readyReplicas}") for arg in cmd):
            return "1"
        if cmd[:3] == ["kubectl", "get", "svc"] and any(arg.startswith("jsonpath={.spec.ports[0].nodePort}") for arg in cmd):
            return "30080"
        if cmd[:3] == ["kubectl", "get", "svc"]:
            return None
        if cmd[:2] == ["kubectl", "get"] and "deployment" in cmd:
            return None
        return None

    return run


def _extract_control(app) -> object:
    for route in app.router.routes:
        if getattr(route, "path", None) == "/users/{user_id}/query":
            if route.endpoint.__closure__:
                return route.endpoint.__closure__[0].cell_contents
    raise RuntimeError("control object not found in route closure")


@contextmanager
def _patch_upstream_client(httpx_module, original_async_client, fake_client_cls):
    # Keep a reference to the real HTTP client for driving ASGI transport requests.
    original_control_httpx_client = httpx_module.AsyncClient
    try:
        httpx_module.AsyncClient = fake_client_cls
        yield original_async_client
    finally:
        # restore original symbol for outer transport client usage
        httpx_module.AsyncClient = original_control_httpx_client


async def _run_flow(app, user_id: str, backend: str) -> list[str]:
    control = _extract_control(app)
    commands: list[str] = []
    # collect raw docker/k8s command strings from patched backend methods
    if backend == "docker":
        import control_plane.app as cp

        cp.DockerSandboxBackend._run = staticmethod(_fake_docker_runner(commands, backend))
    else:
        import control_plane.app as cp

        cp.KubernetesSandboxBackend._run = staticmethod(_fake_k8s_runner(commands, backend))

    import control_plane.app as cp
    original_client = httpx.AsyncClient
    with _patch_upstream_client(cp.httpx, original_client, _FakeAsyncClient):
        transport = httpx.ASGITransport(app=app)
        # use the original client class saved before patching cp.httpx.AsyncClient
        async with original_client(transport=transport, base_url="http://control-plane.local", timeout=None) as client:
            first_query = await client.post(f"/users/{user_id}/query", json={"prompt": "第一轮：创建用户沙箱并触发分配"})
            assert first_query.status_code == 200
            assignment = control._assignments.get(user_id)
            if assignment is None:
                raise RuntimeError(f"assignment missing after query; query result={first_query.text}")

            runtime = control._sandboxes[assignment.sandbox_id]
            workspace = runtime.restore_root / "workspace"
            claude_dir = runtime.restore_root / ".claude"
            workspace.mkdir(parents=True, exist_ok=True)
            claude_dir.mkdir(parents=True, exist_ok=True)
            (workspace / "hello.txt").write_text("v1-workspace")
            (claude_dir / "settings.json").write_text("{\"mock\": true}")

            release_payload = (await client.post(f"/users/{user_id}/release")).json()
            assert release_payload["status"] == "released"

            second_query = await client.post(f"/users/{user_id}/query", json={"prompt": "第二轮：从快照恢复"})
            assert second_query.status_code == 200
            restored = json.loads(second_query.text)
            assert restored["result"] == "mock query result"

            post_assignment = control._assignments[user_id]
            restored_runtime = control._sandboxes[post_assignment.sandbox_id]
            restored_workspace_file = restored_runtime.restore_root / "workspace" / "hello.txt"
            if restored_workspace_file.exists():
                print(f"restore-ok: {restored_workspace_file.name} -> {restored_workspace_file.read_text()}")
            else:
                raise RuntimeError("restore file not found")

    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description="Local control plane docker/k8s dry-run validator")
    parser.add_argument(
        "--backend",
        choices=["docker", "k8s", "all"],
        default="all",
        help="Select one backend or run both (default: all)",
    )
    parser.add_argument("--user", default="user-demo")
    args = parser.parse_args()

    user = args.user

    targets = [args.backend] if args.backend != "all" else ["docker", "k8s"]
    for backend in targets:
        print(f"[dry-run] starting backend={backend}, user={user}")
        with tempfile.TemporaryDirectory(prefix=f"control-plane-dry-run-{backend}-") as workdir:
            root = Path(workdir)
            _set_env(root, backend)

            # Reload app module to ensure env is applied to backend selection.
            if "control_plane.app" in sys.modules:
                module_name = "control_plane.app"
                cp_app = importlib.reload(sys.modules[module_name])
            else:
                cp_app = importlib.import_module("control_plane.app")

            commands = asyncio.run(_run_flow(cp_app.app, user, backend))
            print(f"[dry-run] backend={backend} command trace ({len(commands)} entries)")
            for idx, command in enumerate(commands, 1):
                print(f"{idx:02d}: {command}")
            print(f"[dry-run] backend={backend} finished\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
