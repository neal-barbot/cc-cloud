from __future__ import annotations

from fastapi import APIRouter

from app.models.schemas import AgentOptionsPayload, CapabilitiesResponse, ValidationResponse
from app.services.agent_service import CAPABILITIES, normalize_payload

router = APIRouter()


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse(**CAPABILITIES)


@router.post("/hooks/validate", response_model=ValidationResponse)
async def validate_hooks(req: AgentOptionsPayload) -> ValidationResponse:
    normalized = normalize_payload(req)
    return ValidationResponse(valid=True, normalized={"hooks": normalized.get("hooks", {}), "permissionMode": normalized["permissionMode"]})


@router.post("/agents/validate", response_model=ValidationResponse)
async def validate_agents(req: AgentOptionsPayload) -> ValidationResponse:
    normalized = normalize_payload(req)
    return ValidationResponse(valid=True, normalized={"agents": normalized.get("agents", {})})


@router.post("/mcp/validate", response_model=ValidationResponse)
async def validate_mcp(req: AgentOptionsPayload) -> ValidationResponse:
    normalized = normalize_payload(req)
    return ValidationResponse(valid=True, normalized={"mcpServers": normalized.get("mcpServers", {})})
