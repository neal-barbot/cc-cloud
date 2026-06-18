from __future__ import annotations

import json
from pathlib import Path

import pytest

from control_plane.app import DockerSandboxBackend, KubernetesSandboxBackend, SandboxRuntime


class _FakeDockerState:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.next_id = 1
        self.by_name: dict[str, str] = {}
        self.by_id: dict[str, str] = {}
        self.state: dict[str, str] = {}

    def run(self, cmd: list[str], required: bool = False) -> str | None:
        del required
        self.calls.append(cmd)

        if cmd[:2] == ["docker", "inspect"] and "-f" in cmd and len(cmd) >= 5:
            template_index = cmd.index("-f") + 1
            template = cmd[template_index]
            target = cmd[template_index + 1]
            if template == "{{.Id}}":
                return self.by_name.get(target)
            if template == "{{.State.Status}}":
                return self.state.get(target)
            if template == "{{.State.Running}}":
                state = self.state.get(target, "")
                return "true" if state == "running" else "false"
            if template == "{{json .NetworkSettings.Ports}}":
                return '{"8765/tcp":[{"HostIp":"0.0.0.0","HostPort":"54321"}]}'
            if template == "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}":
                return "127.0.0.1"

        if cmd[:2] == ["docker", "ps"]:
            return None

        if cmd[:2] == ["docker", "create"]:
            name = cmd[3]
            container_id = f"cid-{self.next_id:04d}"
            self.next_id += 1
            self.by_name[name] = container_id
            self.by_id[container_id] = name
            self.state[container_id] = "created"
            return container_id

        if cmd[:2] == ["docker", "start"]:
            container_id = cmd[2]
            self.state[container_id] = "running"
            return container_id

        if cmd[:2] == ["docker", "stop"]:
            container_id = cmd[2]
            self.state[container_id] = "exited"
            return container_id

        if cmd[:2] == ["docker", "rm"]:
            container_id = cmd[-1]
            self.state.pop(container_id, None)
            by_name = self.by_id.pop(container_id, None)
            if by_name:
                self.by_name.pop(by_name, None)
            return container_id

        return None


def test_docker_backend_allocate_start_stop_release_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDockerState()
    monkeypatch.setattr(DockerSandboxBackend, "_run", staticmethod(fake.run))

    backend = DockerSandboxBackend()
    runtime = SandboxRuntime(sandbox_id="u1", endpoint="", restore_root=Path("/tmp/x/u1"))

    endpoint = backend.allocate(runtime, None)
    assert endpoint == "http://127.0.0.1:54321"
    assert runtime.container_id is not None
    assert fake.calls[0][0:2] == ["docker", "inspect"]

    assert backend.heartbeat(runtime) is True

    backend.release(runtime, remove=False)
    assert fake.state[runtime.container_id] == "exited"

    runtime2 = SandboxRuntime(sandbox_id="u1", endpoint="", restore_root=Path("/tmp/x/u1"), container_id=runtime.container_id)
    backend.release(runtime2, remove=True)
    assert runtime2.container_id not in fake.state


def test_k8s_backend_generates_kubernetes_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []
    rendered_manifests: list[str] = []

    def fake(cmd: list[str], required: bool = False) -> str | None:
        calls.append((tuple(cmd), required))
        if cmd[:2] == ["kubectl", "apply"] and "-f" in cmd:
            manifest_path = cmd[cmd.index("-f") + 1]
            rendered_manifests.append(Path(manifest_path).read_text(encoding="utf-8"))
            return ""
        if "get" in cmd and "deployment" in cmd and "jsonpath={.metadata.name}" in cmd:
            return "cp-sandbox-u1"
        if "get" in cmd and "deployment" in cmd and "jsonpath={.status.readyReplicas}" in cmd:
            return "1"
        if "get" in cmd and "svc" in cmd and "jsonpath={.spec.ports[0].nodePort}" in cmd:
            return "30080"
        if cmd[:3] == ["kubectl", "get", "svc"]:
            return None
        return None

    monkeypatch.setattr(KubernetesSandboxBackend, "_run", staticmethod(fake))

    backend = KubernetesSandboxBackend()
    runtime = SandboxRuntime(sandbox_id="u1", endpoint="", restore_root=Path("/tmp/x/u1"))
    endpoint = backend.allocate(runtime, None)
    assert endpoint == "http://127.0.0.1:30080"

    backend.start(runtime, None)
    url = backend.endpoint(runtime, None)
    assert url == "http://127.0.0.1:30080"
    assert backend.heartbeat(runtime) is True
    backend.stop(runtime)
    backend.release(runtime, remove=True)

    command_texts = [" ".join(call[0]) for call in calls]
    assert any("kubectl apply -f" in text for text in command_texts)
    assert rendered_manifests, "kubernetes manifest was not generated"
    manifest = json.loads(rendered_manifests[0])
    assert manifest["kind"] == "Deployment"
    volumes = manifest["spec"]["template"]["spec"]["volumes"]
    mount_names = {volume["name"] for volume in volumes}
    assert {"claude", "workspace"} <= mount_names
    assert any("kubectl scale deployment cp-sandbox-u1 -n default --replicas=1" in text for text in command_texts)
    assert any("kubectl scale deployment cp-sandbox-u1 -n default --replicas=0" in text for text in command_texts)
    assert any("kubectl delete deployment cp-sandbox-u1 -n default --ignore-not-found" in text for text in command_texts)
