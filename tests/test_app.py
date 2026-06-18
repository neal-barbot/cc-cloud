from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.session_service import registry


def _run(coro):
    return asyncio.run(coro)


def _collect_session_events(session) -> list[dict]:
    async def _collect() -> list[dict]:
        events: list[dict] = []
        async for event in session.receive_events(heartbeat_seconds=1):
            events.append(event)
            if event["event"] == "turn_complete":
                break
        return events

    return _run(_collect())


@pytest.fixture(autouse=True)
def mock_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HTTP_MOCK", "true")


@pytest.fixture
def client() -> TestClient:
    with TestClient(app, base_url="http://test") as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_query_sync_returns_events(client: TestClient) -> None:
    response = client.post("/v1/query", json={"prompt": "列出当前目录"})
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "Mock result for: 列出当前目录"
    assert [event["event"] for event in body["events"]] == ["system", "assistant", "result"]


def test_query_stream_returns_sse_events(client: TestClient) -> None:
    with client.stream("POST", "/v1/query/stream", json={"prompt": "stream please"}) as response:
        assert response.status_code == 200
        payload = response.read().decode()
    assert "event: system" in payload
    assert "event: assistant" in payload
    assert "event: result" in payload


def test_session_lifecycle(client: TestClient) -> None:
    create_response = client.post("/v1/sessions/create", json={"allowedTools": ["Read", "Edit"]})
    assert create_response.status_code == 200
    session_id = create_response.json()["session_id"]

    send_response = client.post(
        f"/v1/sessions/{session_id}/send",
        json={"message": "分析 auth 模块"},
    )
    assert send_response.status_code == 200

    session = _run(registry.get(session_id))
    assert session is not None
    events = _collect_session_events(session)

    assert [event["event"] for event in events] == ["system", "assistant", "turn_complete"]
    assert json.dumps({"session_id": session_id}, ensure_ascii=False)[1:-1] in json.dumps(events, ensure_ascii=False)

    close_response = client.delete(f"/v1/sessions/{session_id}")
    assert close_response.status_code == 200
    assert close_response.json() == {"session_id": session_id, "status": "closed"}


def test_capabilities_and_configuration_validation(client: TestClient) -> None:
    capabilities = client.get("/v1/capabilities")
    assert capabilities.status_code == 200
    body = capabilities.json()
    assert "POST /v1/query/stream" in body["endpoints"]
    assert body["supports"]["hooks"] is True
    assert body["supports"]["subagents"] is True
    assert body["supports"]["mcp_servers"] is True

    hooks = client.post(
        "/v1/hooks/validate",
        json={"hooks": {"PreToolUse": [{"matcher": "Write|Edit", "action": "deny", "reason": "no env"}]}},
    )
    assert hooks.status_code == 200
    assert hooks.json()["normalized"]["permissionMode"] == "bypassPermissions"

    agents = client.post(
        "/v1/agents/validate",
        json={"agents": {"code-reviewer": {"prompt": "review", "tools": ["Read", "Glob"]}}},
    )
    assert agents.status_code == 200
    assert "code-reviewer" in agents.json()["normalized"]["agents"]

    mcp = client.post(
        "/v1/mcp/validate",
        json={"mcpServers": {"github": {"command": "npx", "args": ["-y", "server"]}}},
    )
    assert mcp.status_code == 200
    assert "github" in mcp.json()["normalized"]["mcpServers"]


def test_session_accepts_image_content(client: TestClient) -> None:
    create_response = client.post("/v1/sessions/create", json={})
    session_id = create_response.json()["session_id"]

    send_response = client.post(
        f"/v1/sessions/{session_id}/send",
        json={
            "content": [
                {"type": "text", "text": "分析图片"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
            ]
        },
    )
    assert send_response.status_code == 200

    session = _run(registry.get(session_id))
    assert session is not None
    events = _collect_session_events(session)
    assert "分析图片" in json.dumps(events, ensure_ascii=False)
    assert "[image]" in json.dumps(events, ensure_ascii=False)

    client.delete(f"/v1/sessions/{session_id}")
