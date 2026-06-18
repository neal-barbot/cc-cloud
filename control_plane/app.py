from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from control_plane.snapshot_store import LocalSnapshotStore



def _now() -> float:
    return time.time()


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class SandboxRuntime:
    sandbox_id: str
    endpoint: str
    restore_root: Path
    container_id: str | None = None
    status: str = "idle"
    user_id: str | None = None
    snapshot_version: int | None = None
    last_seen_at: float = field(default_factory=_now)


class AllocateRequest(BaseModel):
    user_id: str = Field(alias="userId")
    sandbox_id: str | None = Field(default=None, alias="sandboxId")
    endpoint: str = "http://127.0.0.1:8765"
    restore_root: str | None = Field(default=None, alias="restoreRoot")


class ReleaseRequest(BaseModel):
    user_id: str = Field(alias="userId")
    snapshot_paths: list[str] = Field(default_factory=list, alias="snapshotPaths")


class SandboxAssignment(BaseModel):
    user_id: str
    sandbox_id: str
    endpoint: str
    status: str
    snapshot_version: int | None = None
    last_seen_at: float


class ControlPlaneSnapshotResponse(BaseModel):
    user_id: str
    sandbox_id: str
    status: str
    snapshot: dict[str, Any] | None


class SnapshotPruneRequest(BaseModel):
    keep_last: int = Field(default=1, ge=1, alias="keepLast")


class SandboxStats(BaseModel):
    sandbox_id: str
    endpoint: str
    status: str
    container_id: str | None = None
    user_id: str | None = None
    snapshot_version: int | None = None
    last_seen_at: float


class SandboxBackend:
    def create(self, runtime: SandboxRuntime, endpoint_hint: str | None) -> str:
        raise NotImplementedError

    def start(self, runtime: SandboxRuntime, endpoint_hint: str | None) -> None:
        raise NotImplementedError

    def stop(self, runtime: SandboxRuntime) -> None:
        raise NotImplementedError

    def release(self, runtime: SandboxRuntime, remove: bool = False) -> None:
        raise NotImplementedError

    def remove(self, runtime: SandboxRuntime) -> None:
        self.release(runtime, remove=True)

    def heartbeat(self, runtime: SandboxRuntime) -> None:
        return None

    def allocate(self, runtime: SandboxRuntime, endpoint_hint: str | None) -> str:
        container_id = self.create(runtime, endpoint_hint)
        self.start(runtime, endpoint_hint)
        runtime.container_id = container_id if container_id else runtime.container_id
        return self.endpoint(runtime, endpoint_hint)

    def endpoint(self, runtime: SandboxRuntime, endpoint_hint: str | None) -> str:
        del endpoint_hint
        return runtime.endpoint


class DockerSandboxBackend(SandboxBackend):
    def __init__(self) -> None:
        self.image = os.getenv("SANDBOX_IMAGE", "claude-code-http:latest")
        self.container_port = os.getenv("SANDBOX_CONTAINER_PORT", "8765")
        self.host_network = _env_flag("SANDBOX_DOCKER_USE_HOST_NETWORK", "false")
        self.reuse_stopped_container = _env_flag("SANDBOX_REUSE_STOPPED_CONTAINER", "true")
        extra_args = os.getenv("SANDBOX_DOCKER_ARGS", "")
        self.extra_args = shlex.split(extra_args) if extra_args else []

    def _name(self, runtime: SandboxRuntime) -> str:
        return f"cp-sandbox-{runtime.sandbox_id}"

    def create(self, runtime: SandboxRuntime, endpoint_hint: str | None) -> str:
        del endpoint_hint
        restore_root = runtime.restore_root
        restore_root.mkdir(parents=True, exist_ok=True)
        (restore_root / ".claude").mkdir(parents=True, exist_ok=True)
        (restore_root / "workspace").mkdir(parents=True, exist_ok=True)

        name = self._name(runtime)
        container_id = self._run(["docker", "inspect", "-f", "{{.Id}}", name])
        if container_id is not None:
            runtime.container_id = container_id
            return container_id

        running = self._run(["docker", "ps", "-q", "-f", f"name={name}"], required=False)
        if running:
            runtime.container_id = running.strip()
            return running.strip()

        network_args: list[str]
        if self.host_network:
            network_args = ["--network", "host"]
        else:
            network_args = ["-p", f"0:{self.container_port}"]

        container_id = self._run(
            [
                "docker",
                "create",
                "--name",
                name,
                *network_args,
                "-v",
                f"{restore_root / '.claude'}:/home/admin/.claude",
                "-v",
                f"{restore_root / 'workspace'}:/home/admin/workspace",
                *self.extra_args,
                self.image,
            ],
            required=True,
        )
        runtime.container_id = container_id
        return container_id

    def start(self, runtime: SandboxRuntime, endpoint_hint: str | None) -> None:
        del endpoint_hint
        container_id = runtime.container_id
        if not container_id:
            container_id = self._run(["docker", "inspect", "-f", "{{.Id}}", self._name(runtime)], required=True)
            runtime.container_id = container_id

        status = self._run(["docker", "inspect", "-f", "{{.State.Status}}", container_id], required=True).lower()
        if status == "running":
            return

        if status in {"created", "restarting", "paused", "exited", "dead"}:
            self._run(["docker", "start", container_id], required=False)
            status = self._run(["docker", "inspect", "-f", "{{.State.Status}}", container_id], required=True).lower()
            if status != "running":
                raise HTTPException(status_code=502, detail=f"failed to start docker sandbox {container_id}")
            return

        if status == "removing" and self.reuse_stopped_container:
            runtime.container_id = None
            container_id = self.create(runtime, endpoint_hint)
            self.start(runtime, endpoint_hint)
            return

        raise HTTPException(
            status_code=502,
            detail=f"cannot start sandbox {container_id} from status {status}",
        )

    def stop(self, runtime: SandboxRuntime) -> None:
        if runtime.container_id is None:
            return
        self._run(["docker", "stop", "-t", "5", runtime.container_id], required=False)

    def release(self, runtime: SandboxRuntime, remove: bool = False) -> None:
        if runtime.container_id is None:
            return
        if remove:
            self._run(["docker", "rm", "-f", runtime.container_id], required=False)
            runtime.container_id = None
        else:
            self._run(["docker", "stop", runtime.container_id], required=False)

    def heartbeat(self, runtime: SandboxRuntime) -> bool:
        if runtime.container_id is None:
            return False
        status = self._run(["docker", "inspect", "-f", "{{.State.Running}}", runtime.container_id])
        return bool(status and status.strip().lower() == "true")

    def endpoint(self, runtime: SandboxRuntime, endpoint_hint: str | None) -> str:
        del endpoint_hint
        if runtime.container_id is None:
            raise HTTPException(status_code=502, detail="container missing after allocate")

        if self.host_network:
            ip = self._run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    runtime.container_id,
                ],
                required=True,
            ).strip()
            return f"http://{ip}:{self.container_port}"

        ports = self._run(
            [
                "docker",
                "inspect",
                "-f",
                '{{json .NetworkSettings.Ports}}',
                runtime.container_id,
            ],
            required=True,
        )
        host_port = self._extract_host_port_from_json(ports, self.container_port)
        if not host_port:
            raise HTTPException(status_code=503, detail=f"no mapped host port for sandbox {runtime.container_id}")
        return f"http://127.0.0.1:{host_port}"

    @staticmethod
    def _extract_host_port_from_json(payload: str, container_port: str) -> str | None:
        try:
            ports = json.loads(payload or "{}")
        except json.JSONDecodeError:
            return None
        target = f"{container_port}/tcp"
        candidates = ports.get(target)
        if not isinstance(candidates, list) or not candidates:
            return None
        host_port = candidates[0].get("HostPort")
        if isinstance(host_port, int):
            return str(host_port)
        if not isinstance(host_port, str):
            return None

        # Some docker output variants include extra symbols/brackets; extract the first numeric token only.
        match = re.search(r"\b([0-9]{1,5})\b", host_port)
        if not match:
            return None
        port = match.group(1)
        if port.startswith("0") and len(port) > 1:
            port = port.lstrip("0") or "0"
        return port
        return None

    @staticmethod
    def _run(cmd: list[str], required: bool = False) -> str | None:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            if required:
                raise HTTPException(status_code=502, detail=f"docker command failed: {' '.join(cmd)}; {result.stdout}")
            return None
        return result.stdout.strip()


class KubernetesSandboxBackend(SandboxBackend):
    def __init__(self) -> None:
        self.image = os.getenv("SANDBOX_IMAGE", "claude-code-http:latest")
        self.namespace = os.getenv("K8S_NAMESPACE", "default")
        self.node = os.getenv("K8S_NODE_IP", "127.0.0.1")
        self.http_port = os.getenv("SANDBOX_CONTAINER_PORT", "8765")
        self.sandbox_label = "com.claude.control-plane/sandbox"
        self.revision = os.getenv("SANDBOX_K8S_REVISION", "v1")
        self.runtime_replicas = int(os.getenv("SANDBOX_K8S_REPLICAS", "1"))

    @staticmethod
    def _name(runtime: SandboxRuntime) -> str:
        return f"cp-sandbox-{runtime.sandbox_id}"

    @staticmethod
    def _service_name(runtime: SandboxRuntime) -> str:
        return f"cp-sandbox-{runtime.sandbox_id}-svc"

    def create(self, runtime: SandboxRuntime, endpoint_hint: str | None) -> str:
        del endpoint_hint
        name = self._name(runtime)
        if self._run(["kubectl", "get", "deployment", name, "-n", self.namespace], required=False) is None:
            manifest = self._k8s_deployment_manifest(runtime)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
                handle.write(manifest)
                manifest_path = handle.name
            try:
                self._run(["kubectl", "apply", "-f", manifest_path], required=True)
            finally:
                os.unlink(manifest_path)
            self._run(
                [
                    "kubectl",
                    "label",
                    "deployment",
                    name,
                    "-n",
                    self.namespace,
                    f"{self.sandbox_label}=true",
                    f"revision={self.revision}",
                    "--overwrite",
                ],
                required=True,
            )

        self._run(["kubectl", "scale", "deployment", name, "-n", self.namespace, "--replicas=0"], required=False)
        return self._run(["kubectl", "get", "deployment", name, "-n", self.namespace, "-o", "jsonpath={.metadata.name}"], required=True)

    def start(self, runtime: SandboxRuntime, endpoint_hint: str | None) -> None:
        del endpoint_hint
        self._run(
            [
                "kubectl",
                "scale",
                "deployment",
                self._name(runtime),
                "-n",
                self.namespace,
                f"--replicas={max(1, self.runtime_replicas)}",
            ],
            required=False,
        )

    def stop(self, runtime: SandboxRuntime) -> None:
        self._run(["kubectl", "scale", "deployment", self._name(runtime), "-n", self.namespace, "--replicas=0"], required=False)

    def endpoint(self, runtime: SandboxRuntime, endpoint_hint: str | None) -> str:
        del endpoint_hint
        service_name = self._service_name(runtime)

        if self._run(["kubectl", "get", "svc", service_name, "-n", self.namespace], required=False) is None:
            self._run(
                [
                    "kubectl",
                    "expose",
                    "deployment",
                    self._name(runtime),
                    "-n",
                    self.namespace,
                    "--name",
                    service_name,
                    "--type",
                    "NodePort",
                    "--port",
                    self.http_port,
                    "--target-port",
                    self.http_port,
                ],
                required=True,
            )

        node_port = self._run(
            [
                "kubectl",
                "get",
                "svc",
                service_name,
                "-n",
                self.namespace,
                "-o",
                "jsonpath={.spec.ports[0].nodePort}",
            ],
            required=True,
        )
        return f"http://{self.node}:{node_port.strip()}"

    def heartbeat(self, runtime: SandboxRuntime) -> bool:
        desired = self._run(
            [
                "kubectl",
                "get",
                "deployment",
                self._name(runtime),
                "-n",
                self.namespace,
                "-o",
                "jsonpath={.status.readyReplicas}",
            ]
        )
        if not desired:
            return False
        try:
            return int(desired.strip()) >= 1
        except ValueError:
            return False

    def release(self, runtime: SandboxRuntime, remove: bool = False) -> None:
        name = self._name(runtime)
        service_name = self._service_name(runtime)
        self._run(["kubectl", "delete", "svc", service_name, "-n", self.namespace, "--ignore-not-found"], required=False)
        if remove:
            self._run(["kubectl", "delete", "deployment", name, "-n", self.namespace, "--ignore-not-found"], required=False)
        else:
            self.stop(runtime)

    def _k8s_deployment_manifest(self, runtime: SandboxRuntime) -> str:
        name = self._name(runtime)
        restore_root = runtime.restore_root
        restore_root.mkdir(parents=True, exist_ok=True)
        (restore_root / ".claude").mkdir(parents=True, exist_ok=True)
        (restore_root / "workspace").mkdir(parents=True, exist_ok=True)

        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": {
                    "app": name,
                    self.sandbox_label: "true",
                    "revision": self.revision,
                },
            },
            "spec": {
                "replicas": 0,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {
                        "labels": {
                            "app": name,
                            self.sandbox_label: "true",
                            "revision": self.revision,
                        },
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "sandbox",
                                "image": self.image,
                                "ports": [{"containerPort": int(self.http_port)}],
                                "volumeMounts": [
                                    {"name": "claude", "mountPath": "/home/admin/.claude"},
                                    {"name": "workspace", "mountPath": "/home/admin/workspace"},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "claude",
                                "hostPath": {
                                    "path": str((restore_root / ".claude").resolve()),
                                    "type": "DirectoryOrCreate",
                                },
                            },
                            {
                                "name": "workspace",
                                "hostPath": {
                                    "path": str((restore_root / "workspace").resolve()),
                                    "type": "DirectoryOrCreate",
                                },
                            },
                        ],
                    },
                },
            },
        }
        return json.dumps(manifest, ensure_ascii=False, indent=2)

    @staticmethod
    def _run(cmd: list[str], required: bool = False) -> str | None:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=45,
        )
        if result.returncode != 0:
            if required:
                raise HTTPException(status_code=502, detail=f"kubectl command failed: {' '.join(cmd)}; {result.stdout}")
            return None
        return result.stdout.strip()


class SandboxControl:
    def __init__(self) -> None:
        self.snapshot_store = LocalSnapshotStore(
            os.getenv("SNAPSHOT_ROOT", "/tmp/claude-code-snapshots"),
            os.getenv("SNAPSHOT_BACKEND_URI"),
        )
        self.sandbox_state_root = Path(os.getenv("SANDBOX_STATE_ROOT", "/tmp/claude-sandboxes")).resolve()
        self.sandbox_state_root.mkdir(parents=True, exist_ok=True)
        self._state_file = self.sandbox_state_root / "control_state.json"

        self.idle_timeout_seconds = int(os.getenv("SANDBOX_IDLE_TIMEOUT_SECONDS", "1800"))
        self.reap_interval_seconds = int(os.getenv("SANDBOX_REAP_INTERVAL_SECONDS", "20"))
        self.remove_on_release = _env_flag("SANDBOX_REMOVE_ON_RELEASE", "false")
        self.wait_for_sandbox_health = _env_flag("SANDBOX_WAIT_FOR_HEALTH", "false")
        self.health_check_retries = int(os.getenv("SANDBOX_HEALTH_CHECK_RETRIES", "20"))
        self.health_check_interval = float(os.getenv("SANDBOX_HEALTH_CHECK_INTERVAL_SECONDS", "0.75"))
        self.purge_restore_root_on_idle = _env_flag("SANDBOX_PURGE_ON_IDLE", "false")

        backend = os.getenv("SANDBOX_BACKEND", "docker").strip().lower()
        if backend == "k8s":
            self.backend = KubernetesSandboxBackend()
        else:
            self.backend = DockerSandboxBackend()

        pool_size = int(os.getenv("SANDBOX_POOL_SIZE", "0"))
        self._sandboxes: dict[str, SandboxRuntime] = {}
        self._assignments: dict[str, SandboxAssignment] = {}
        for idx in range(1, max(0, pool_size) + 1):
            sandbox_id = f"sandbox-{idx}"
            restore_root = self.sandbox_state_root / sandbox_id
            self._sandboxes[sandbox_id] = SandboxRuntime(
                sandbox_id=sandbox_id,
                endpoint="",
                restore_root=restore_root,
            )

        self._next_sandbox_suffix = pool_size + 1
        self._reaper_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_file.exists():
            return
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        for sid, item in payload.get("sandboxes", {}).items():
            existing = self._sandboxes.get(sid)
            if existing is None:
                existing = SandboxRuntime(
                    sandbox_id=sid,
                    endpoint="",
                    restore_root=Path(item.get("restore_root", self.sandbox_state_root / sid)),
                )
                self._sandboxes[sid] = existing

            existing.endpoint = str(item.get("endpoint", ""))
            existing.container_id = item.get("container_id")
            existing.status = str(item.get("status", "idle"))
            existing.user_id = item.get("user_id")
            existing.snapshot_version = item.get("snapshot_version")
            existing.last_seen_at = float(item.get("last_seen_at", _now()))
            existing.restore_root = Path(item.get("restore_root", existing.restore_root))

            if sid.startswith("sandbox-"):
                try:
                    idx = int(sid.split("-")[-1]) + 1
                    if idx > self._next_sandbox_suffix:
                        self._next_sandbox_suffix = idx
                except ValueError:
                    pass

        for user_id, item in payload.get("assignments", {}).items():
            try:
                self._assignments[user_id] = SandboxAssignment(**item)
            except Exception:
                continue

    def _persist_state(self) -> None:
        payload = {
            "sandboxes": {},
            "assignments": {},
        }
        for sid, sandbox in self._sandboxes.items():
            payload["sandboxes"][sid] = {
                "restore_root": str(sandbox.restore_root),
                "endpoint": sandbox.endpoint,
                "container_id": sandbox.container_id,
                "status": sandbox.status,
                "user_id": sandbox.user_id,
                "snapshot_version": sandbox.snapshot_version,
                "last_seen_at": sandbox.last_seen_at,
            }

        for user_id, assignment in self._assignments.items():
            payload["assignments"][user_id] = assignment.model_dump()

        self._state_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _snapshot_paths(restore_root: Path) -> list[str]:
        return [str(restore_root / ".claude"), str(restore_root / "workspace")]

    def _coerce_snapshot_paths(self, restore_root: Path, requested: list[str] | None = None) -> list[str]:
        if not requested:
            return self._snapshot_paths(restore_root)

        normalized = []
        allowed: set[str] = set(self._snapshot_paths(restore_root))
        allowed_dirs = {Path(path).resolve() for path in allowed}
        for item in requested:
            candidate = Path(item).expanduser()
            if not candidate.is_absolute():
                candidate = restore_root / candidate
            candidate = candidate.resolve()
            if str(candidate) in allowed:
                normalized.append(str(candidate))
                continue

            shortcut = (restore_root / candidate.name).resolve()
            if shortcut in allowed_dirs:
                normalized.append(str(shortcut))

        return normalized or self._snapshot_paths(restore_root)

    async def get_or_allocate(self, req: AllocateRequest) -> SandboxRuntime:
        async with self._lock:
            existing = self._assignments.get(req.user_id)
            if existing is not None:
                sandbox = self._sandboxes.get(existing.sandbox_id)
                if sandbox is None:
                    self._assignments.pop(req.user_id, None)
                elif sandbox.user_id == req.user_id and sandbox.status == "active":
                    if await asyncio.to_thread(self.backend.heartbeat, sandbox):
                        sandbox.last_seen_at = _now()
                        sandbox.status = "active"
                        self._assignments[req.user_id] = self._to_assignment(sandbox)
                        self._persist_state()
                        return sandbox
                    sandbox.status = "idle"
                    sandbox.user_id = None
                    self._assignments.pop(req.user_id, None)

            target_sandbox = self._select_or_create_sandbox(req)
            requested_restore_root = Path(req.restore_root).expanduser() if req.restore_root else None
            if requested_restore_root is not None:
                target_sandbox.restore_root = requested_restore_root

            if target_sandbox.restore_root.exists():
                shutil.rmtree(target_sandbox.restore_root, ignore_errors=True)
            target_sandbox.restore_root.mkdir(parents=True, exist_ok=True)

            latest_snapshot = self.snapshot_store.restore(req.user_id, target_sandbox.restore_root)
            target_sandbox.snapshot_version = latest_snapshot.version if latest_snapshot else None

            endpoint = await asyncio.to_thread(self.backend.allocate, target_sandbox, req.endpoint)
            if self.wait_for_sandbox_health:
                await asyncio.to_thread(self._wait_for_health, endpoint)

            target_sandbox.endpoint = endpoint
            target_sandbox.status = "active"
            target_sandbox.user_id = req.user_id
            target_sandbox.last_seen_at = _now()

            self._assignments[req.user_id] = self._to_assignment(target_sandbox)
            self._persist_state()
            return target_sandbox

    def _wait_for_health(self, endpoint: str) -> None:
        attempts = 0
        last_err: Exception | None = None
        target = endpoint.rstrip("/") + "/healthz"
        while attempts < max(1, self.health_check_retries):
            attempts += 1
            try:
                # Avoid inheriting host environment proxy settings into sandbox probe URLs.
                with httpx.Client(timeout=1.0, trust_env=False) as client:
                    response = client.get(target)
                    if response.status_code < 500:
                        return
            except Exception as exc:  # pragma: no cover
                last_err = exc
            time.sleep(self.health_check_interval)

        if last_err is not None:
            raise HTTPException(
                status_code=503,
                detail=f"sandbox not ready: {target}; last error: {type(last_err).__name__}: {last_err}",
            )
        raise HTTPException(
            status_code=503,
            detail=f"sandbox not ready: {target} (after health check timeout)",
        )

    async def release(self, req: ReleaseRequest) -> tuple[dict[str, Any], str, str]:
        async with self._lock:
            assignment = self._assignments.pop(req.user_id, None)
            if assignment is None:
                raise HTTPException(status_code=404, detail="assignment not found")

            sandbox = self._sandboxes[assignment.sandbox_id]
            snapshot_paths = self._coerce_snapshot_paths(sandbox.restore_root, req.snapshot_paths)
            record = self.snapshot_store.save(req.user_id, snapshot_paths)

            sandbox.user_id = None
            sandbox.status = "idle"
            sandbox.last_seen_at = _now()
            if record.version:
                sandbox.snapshot_version = record.version
            self._persist_state()

            if self.purge_restore_root_on_idle:
                shutil.rmtree(sandbox.restore_root, ignore_errors=True)

        await asyncio.to_thread(self.backend.release, sandbox, self.remove_on_release)
        return {
            "user_id": req.user_id,
            "sandbox_id": sandbox.sandbox_id,
            "status": "released",
            "snapshot": record.__dict__,
        }, sandbox.sandbox_id, req.user_id

    def _select_or_create_sandbox(self, req: AllocateRequest) -> SandboxRuntime:
        if req.sandbox_id and req.sandbox_id in self._sandboxes:
            sandbox = self._sandboxes[req.sandbox_id]
            if sandbox.status == "active" and sandbox.user_id != req.user_id:
                raise HTTPException(status_code=409, detail="sandbox occupied")
            return sandbox

        if req.sandbox_id:
            sandbox = SandboxRuntime(
                sandbox_id=req.sandbox_id,
                endpoint=req.endpoint,
                restore_root=self.sandbox_state_root / req.sandbox_id,
            )
            self._sandboxes[req.sandbox_id] = sandbox
            self._persist_state()
            return sandbox

        sandbox = self._find_idle_sandbox(req.endpoint)
        if sandbox is not None:
            return sandbox

        sandbox_id = f"sandbox-{self._next_sandbox_suffix}"
        self._next_sandbox_suffix += 1
        sandbox = SandboxRuntime(
            sandbox_id=sandbox_id,
            endpoint=req.endpoint,
            restore_root=self.sandbox_state_root / sandbox_id,
        )
        self._sandboxes[sandbox_id] = sandbox
        self._persist_state()
        return sandbox

    def _find_idle_sandbox(self, preferred_endpoint: str) -> SandboxRuntime | None:
        for sandbox in self._sandboxes.values():
            if sandbox.status != "idle":
                continue
            if not preferred_endpoint or not sandbox.endpoint or sandbox.endpoint == preferred_endpoint:
                return sandbox
        for sandbox in self._sandboxes.values():
            if sandbox.status == "idle":
                return sandbox
        return None

    async def get_assignment(self, user_id: str) -> SandboxAssignment:
        async with self._lock:
            assignment = self._assignments.get(user_id)
            if assignment is None:
                raise HTTPException(status_code=404, detail="assignment not found")
            sandbox = self._sandboxes.get(assignment.sandbox_id)
            if sandbox is None:
                self._assignments.pop(user_id, None)
                self._persist_state()
                raise HTTPException(status_code=410, detail="sandbox missing")

            sandbox.last_seen_at = _now()
            updated = self._to_assignment(sandbox)
            self._assignments[user_id] = updated
            self._persist_state()
            return updated

    def latest_snapshot(self, user_id: str) -> dict[str, Any]:
        meta = self.snapshot_store.latest_snapshot_meta(user_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        return meta

    def compact_snapshot(self, user_id: str) -> dict[str, Any]:
        record = self.snapshot_store.compact(user_id)
        if record is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        return record.__dict__

    def prune_snapshots(self, user_id: str, keep_last: int = 1) -> dict[str, Any]:
        removed = self.snapshot_store.prune(user_id, keep_last=keep_last)
        latest = self.snapshot_store.latest_snapshot_meta(user_id)
        return {
            "user_id": user_id,
            "keep_last": keep_last,
            "removed_versions": [record.version for record in removed],
            "latest": latest,
        }

    def list_sandboxes(self) -> list[SandboxStats]:
        return [
            SandboxStats(
                sandbox_id=sandbox_id,
                endpoint=sandbox.endpoint,
                status=sandbox.status,
                container_id=sandbox.container_id,
                user_id=sandbox.user_id,
                snapshot_version=sandbox.snapshot_version,
                last_seen_at=sandbox.last_seen_at,
            )
            for sandbox_id, sandbox in sorted(self._sandboxes.items())
        ]

    def _to_assignment(self, sandbox: SandboxRuntime) -> SandboxAssignment:
        return SandboxAssignment(
            user_id=sandbox.user_id or "",
            sandbox_id=sandbox.sandbox_id,
            endpoint=sandbox.endpoint,
            status=sandbox.status,
            snapshot_version=sandbox.snapshot_version,
            last_seen_at=sandbox.last_seen_at,
        )

    async def reap_idle(self) -> None:
        while True:
            await asyncio.sleep(max(5, self.reap_interval_seconds))
            now = _now()
            to_release: list[tuple[SandboxRuntime, bool]] = []
            for sandbox in list(self._sandboxes.values()):
                if sandbox.status != "active":
                    continue
                if sandbox.user_id is None:
                    sandbox.status = "idle"
                    self._persist_state()
                    continue

                async with self._lock:
                    if not await asyncio.to_thread(self.backend.heartbeat, sandbox):
                        user_id = sandbox.user_id
                        sandbox.user_id = None
                        sandbox.status = "idle"
                        sandbox.last_seen_at = now
                        if user_id:
                            self._assignments.pop(user_id, None)
                            self.snapshot_store.save(user_id, self._snapshot_paths(sandbox.restore_root))
                        self._persist_state()
                        to_release.append((sandbox, self.remove_on_release))
                        continue

                    if now - sandbox.last_seen_at <= self.idle_timeout_seconds:
                        continue

                    assignment = self._assignments.pop(sandbox.user_id, None)
                    if assignment is None:
                        sandbox.user_id = None
                        sandbox.status = "idle"
                        self._persist_state()
                        continue

                    self.snapshot_store.save(assignment.user_id, self._snapshot_paths(sandbox.restore_root))
                    sandbox.user_id = None
                    sandbox.status = "idle"
                    sandbox.last_seen_at = now
                    self._persist_state()
                    to_release.append((sandbox, self.remove_on_release))

            for sandbox, remove in to_release:
                await asyncio.to_thread(self.backend.release, sandbox, remove)

    def attach_lifecycle(self, app: FastAPI) -> None:
        @app.on_event("startup")
        async def start_reaper() -> None:
            if self._reaper_task is None:
                self._reaper_task = asyncio.create_task(self.reap_idle())

        @app.on_event("shutdown")
        async def stop_reaper() -> None:
            if self._reaper_task and not self._reaper_task.done():
                self._reaper_task.cancel()
                try:
                    await self._reaper_task
                except asyncio.CancelledError:
                    pass


def _forward_headers(client_headers: Any) -> dict[str, str]:
    ignored = {"host", "content-length", "connection", "accept-encoding"}
    return {
        key: value
        for key, value in client_headers.items()
        if key.lower() not in ignored
    }


def _copy_response_headers(headers: Any) -> dict[str, str]:
    ignored = {"content-length", "transfer-encoding", "connection", "content-encoding"}
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in ignored
    }


def _build_control_plane_app() -> FastAPI:
    app = FastAPI(title="Sandbox Control Plane", version="0.3.0")
    control = SandboxControl()

    @app.post("/control/allocate", response_model=SandboxAssignment)
    async def allocate(req: AllocateRequest) -> SandboxAssignment:
        sandbox = await control.get_or_allocate(req)
        return control._to_assignment(sandbox)

    @app.get("/control/assignments/{user_id}", response_model=SandboxAssignment)
    async def get_assignment(user_id: str) -> SandboxAssignment:
        return await control.get_assignment(user_id)

    @app.get("/control/sandboxes")
    async def list_sandboxes() -> list[SandboxStats]:
        return control.list_sandboxes()

    @app.post("/control/release", response_model=ControlPlaneSnapshotResponse)
    async def release(req: ReleaseRequest) -> ControlPlaneSnapshotResponse:
        payload, _, _ = await control.release(req)
        return ControlPlaneSnapshotResponse(**payload)

    @app.post("/users/{user_id}/release", response_model=ControlPlaneSnapshotResponse)
    async def user_release(user_id: str) -> ControlPlaneSnapshotResponse:
        payload, _, _ = await control.release(ReleaseRequest(userId=user_id))
        return ControlPlaneSnapshotResponse(**payload)

    @app.get("/control/snapshots/{user_id}")
    async def latest_snapshot(user_id: str) -> dict[str, Any]:
        return control.latest_snapshot(user_id)

    @app.post("/control/snapshots/{user_id}/compact")
    async def compact_snapshot(user_id: str) -> dict[str, Any]:
        return control.compact_snapshot(user_id)

    @app.post("/control/snapshots/{user_id}/prune")
    async def prune_snapshots(user_id: str, req: SnapshotPruneRequest | None = None) -> dict[str, Any]:
        keep_last = req.keep_last if req else 1
        return control.prune_snapshots(user_id, keep_last=keep_last)

    @app.post("/users/{user_id}/query")
    async def user_query(user_id: str, request: Request) -> Response:
        body = await request.body()
        assignment_request = AllocateRequest(userId=user_id)
        sandbox = await control.get_or_allocate(assignment_request)
        forward_headers = _forward_headers(request.headers)
        target = f"{sandbox.endpoint.rstrip('/')}/v1/query"
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                upstream = await client.post(
                    target,
                    content=body,
                    headers=forward_headers,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={"content-type": upstream.headers.get("content-type", "application/json")},
        )

    @app.post("/users/{user_id}/query/stream")
    async def user_query_stream(user_id: str, request: Request) -> StreamingResponse:
        body = await request.body()
        assignment_request = AllocateRequest(userId=user_id)
        sandbox = await control.get_or_allocate(assignment_request)
        target = f"{sandbox.endpoint.rstrip('/')}/v1/query/stream"
        forward_headers = _forward_headers(request.headers)

        async def event_generator() -> Any:
            try:
                async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                    async with client.stream("POST", target, content=body, headers=forward_headers) as resp:
                        if resp.status_code >= 400:
                            err = await resp.aread()
                            raise HTTPException(
                                status_code=resp.status_code,
                                detail=err.decode(errors="ignore"),
                            )
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                yield chunk
            except HTTPException:
                raise
            except Exception as exc:  # pragma: no cover
                raise HTTPException(status_code=502, detail=f"upstream stream error: {exc}") from exc

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache"},
        )

    @app.api_route(
        "/users/{user_id}/v1/{sandbox_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def user_v1_proxy(user_id: str, sandbox_path: str, request: Request):
        body = await request.body()
        assignment_request = AllocateRequest(userId=user_id)
        sandbox = await control.get_or_allocate(assignment_request)
        target = f"{sandbox.endpoint.rstrip('/')}/v1/{sandbox_path.lstrip('/')}"
        forward_headers = _forward_headers(request.headers)
        query = request.url.query
        if query:
            target = f"{target}?{query}"

        wants_stream = (
            sandbox_path.endswith("/events")
            or sandbox_path.endswith("/stream")
            or "text/event-stream" in request.headers.get("accept", "")
        )

        if wants_stream:
            async def event_generator() -> Any:
                try:
                    async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                        async with client.stream(
                            request.method,
                            target,
                            content=body,
                            headers=forward_headers,
                        ) as resp:
                            if resp.status_code >= 400:
                                err = await resp.aread()
                                raise HTTPException(
                                    status_code=resp.status_code,
                                    detail=err.decode(errors="ignore"),
                                )
                            async for chunk in resp.aiter_bytes():
                                if chunk:
                                    yield chunk
                except HTTPException:
                    raise
                except Exception as exc:  # pragma: no cover
                    raise HTTPException(status_code=502, detail=f"upstream stream error: {exc}") from exc

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache"},
            )

        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                upstream = await client.request(
                    request.method,
                    target,
                    content=body,
                    headers=forward_headers,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=_copy_response_headers(upstream.headers),
        )

    control.attach_lifecycle(app)
    return app


app = _build_control_plane_app()
