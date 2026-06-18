from __future__ import annotations

import json

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.models.schemas import QueryRequest, QueryResponse
from app.services.agent_service import run_query, run_query_stream

router = APIRouter()


@router.post("/query", response_model=QueryResponse)
async def query_once(req: QueryRequest) -> QueryResponse:
    events = await run_query(req)
    result_event = next((event for event in reversed(events) if event.get("event") == "result"), None)
    data = result_event.get("data", {}) if result_event else {}
    return QueryResponse(events=events, result=data.get("result"), session_id=data.get("session_id"))


@router.post("/query/stream")
async def query_stream(req: QueryRequest) -> EventSourceResponse:
    async def event_generator():
        async for event in run_query_stream(req):
            yield {
                "event": event.get("event", "message"),
                "data": json.dumps(event.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())
