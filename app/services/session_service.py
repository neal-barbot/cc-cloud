from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from app.models.schemas import CreateSessionRequest, MessagePart, SendMessageRequest, SessionInfo
from app.services.agent_service import (
    ClaudeSDKClient,
    build_options,
    serialize_message,
    sdk_available,
    use_mock_backend,
)


@dataclass
class StreamingSession:
    session_id: str
    request: CreateSessionRequest
    options: Any
    client: Any = None
    running: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    message_queue: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    response_queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None

    async def start(self) -> None:
        self.running = True
        if use_mock_backend() or not sdk_available():
            self.task = asyncio.create_task(self._mock_process_loop())
            await self.response_queue.put({"event": "system", "data": {"subtype": "init", "session_id": self.session_id, "details": {"backend": "mock"}}})
            return

        self.client = ClaudeSDKClient(options=self.options)
        await self.client.__aenter__()
        self.task = asyncio.create_task(self._process_loop())

    async def send(self, req: SendMessageRequest) -> None:
        if not self.running:
            raise RuntimeError("session is closed")
        self.updated_at = time.time()
        await self.message_queue.put(_message_to_sdk_payload(req))

    async def close(self) -> None:
        if not self.running:
            return
        self.running = False
        await self.message_queue.put(None)
        if self.task:
            await asyncio.wait([self.task], timeout=5)
        if self.client is not None:
            await self.client.__aexit__(None, None, None)
        await self.response_queue.put({"event": "closed", "data": {"session_id": self.session_id}})

    async def receive_events(self, heartbeat_seconds: int = 120) -> AsyncGenerator[dict[str, Any], None]:
        while self.running or not self.response_queue.empty():
            try:
                yield await asyncio.wait_for(self.response_queue.get(), timeout=heartbeat_seconds)
            except asyncio.TimeoutError:
                yield {"event": "heartbeat", "data": {"session_id": self.session_id}}

    async def _process_loop(self) -> None:
        while self.running:
            message = await self.message_queue.get()
            if message is None:
                break
            try:
                await self.client.query(message)
                async for response in self.client.receive_response():
                    await self.response_queue.put(serialize_message(response))
                await self.response_queue.put({"event": "turn_complete", "data": {"session_id": self.session_id}})
            except Exception as exc:
                await self.response_queue.put(
                    {
                        "event": "error",
                        "data": {"session_id": self.session_id, "type": type(exc).__name__, "message": str(exc)},
                    }
                )

    async def _mock_process_loop(self) -> None:
        while self.running:
            message = await self.message_queue.get()
            if message is None:
                break
            display = _message_display_text(message)
            await self.response_queue.put(
                {
                    "event": "assistant",
                    "data": {"content": [{"type": "text", "text": f"Mock session reply: {display}"}], "parent_tool_use_id": None},
                }
            )
            await self.response_queue.put({"event": "turn_complete", "data": {"session_id": self.session_id}})

    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            status="running" if self.running else "closed",
            queued_messages=self.message_queue.qsize(),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, StreamingSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, req: CreateSessionRequest) -> StreamingSession:
        session_id = str(uuid.uuid4())
        session = StreamingSession(session_id=session_id, request=req, options=build_options(req))
        await session.start()
        async with self._lock:
            self._sessions[session_id] = session
        return session

    async def get(self, session_id: str) -> StreamingSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def list(self) -> list[SessionInfo]:
        async with self._lock:
            return [session.info() for session in self._sessions.values()]

    async def close(self, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        await session.close()
        return True


registry = SessionRegistry()


def _message_to_sdk_payload(req: SendMessageRequest) -> Any:
    if req.content:
        content: list[dict[str, Any]] = []
        for part in req.content:
            if part.type == "text":
                content.append({"type": "text", "text": part.text or ""})
            elif part.type == "image":
                source = part.source or {}
                content.append({"type": "image", "source": source})
        return {"type": "user", "message": {"role": "user", "content": content}}
    return req.message or ""


def _message_display_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        parts = message.get("message", {}).get("content", [])
        texts = [part.get("text", "[image]") for part in parts if isinstance(part, dict)]
        return " ".join(texts)
    return str(message)
