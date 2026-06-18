from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.models.schemas import CloseSessionResponse, CreateSessionRequest, CreateSessionResponse, SendMessageRequest, SessionInfo
from app.services.session_service import registry

router = APIRouter()


@router.post("/sessions/create", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
    session = await registry.create(req)
    return CreateSessionResponse(session_id=session.session_id, status="running")


@router.get("/sessions", response_model=list[SessionInfo])
async def list_sessions() -> list[SessionInfo]:
    return await registry.list()


@router.get("/sessions/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str) -> SessionInfo:
    session = await registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session.info()


@router.post("/sessions/{session_id}/send")
async def send_message(session_id: str, req: SendMessageRequest) -> dict[str, str]:
    session = await registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    await session.send(req)
    return {"status": "queued", "session_id": session_id}


@router.get("/sessions/{session_id}/events")
async def session_events(session_id: str) -> EventSourceResponse:
    session = await registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    async def event_generator():
        async for event in session.receive_events():
            yield {
                "event": event.get("event", "message"),
                "data": json.dumps(event.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


@router.delete("/sessions/{session_id}", response_model=CloseSessionResponse)
async def close_session(session_id: str) -> CloseSessionResponse:
    closed = await registry.close(session_id)
    if not closed:
        raise HTTPException(status_code=404, detail="session not found")
    return CloseSessionResponse(session_id=session_id, status="closed")
