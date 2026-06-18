from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PermissionMode = Literal["default", "dontAsk", "acceptEdits", "bypassPermissions", "plan", "auto"]
HookEventName = Literal[
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "Notification",
    "SubagentStart",
    "PermissionRequest",
]


class HookSpec(BaseModel):
    matcher: str | None = None
    action: Literal["allow", "deny", "webhook", "log"] = "log"
    reason: str | None = None
    webhook_url: str | None = None
    timeout: float | None = None


class AgentDefinition(BaseModel):
    description: str | None = None
    prompt: str
    tools: list[str] = Field(default_factory=list)
    model: str | None = None


class AgentOptionsPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tools: list[str] = Field(default_factory=list)
    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    cwd: str | None = None
    max_turns: int | None = Field(default=None, alias="maxTurns")
    max_budget_usd: float | None = Field(default=None, alias="maxBudgetUsd")
    allowed_tools: list[str] = Field(default_factory=list, alias="allowedTools")
    disallowed_tools: list[str] = Field(default_factory=list, alias="disallowedTools")
    permission_mode: PermissionMode | None = Field(default=None, alias="permissionMode")
    mcp_servers: dict[str, Any] = Field(default_factory=dict, alias="mcpServers")
    hooks: dict[HookEventName, list[HookSpec]] = Field(default_factory=dict)
    agents: dict[str, AgentDefinition] = Field(default_factory=dict)
    skills: list[str] | Literal["all"] | None = None
    plugins: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None
    fallback_model: str | None = Field(default=None, alias="fallbackModel")
    include_partial_messages: bool | None = Field(default=None, alias="includePartialMessages")
    include_hook_events: bool | None = Field(default=None, alias="includeHookEvents")
    continue_conversation: bool | None = Field(default=None, alias="continueConversation")
    resume: str | None = None
    session_id: str | None = Field(default=None, alias="sessionId")
    add_dirs: list[str] = Field(default_factory=list, alias="addDirs")
    env: dict[str, str] = Field(default_factory=dict)
    extra_args: dict[str, str | None] = Field(default_factory=dict, alias="extraArgs")
    extra: dict[str, Any] = Field(default_factory=dict)


class QueryRequest(AgentOptionsPayload):
    prompt: str
    stream: bool | None = None


class MessagePart(BaseModel):
    type: Literal["text", "image"] = "text"
    text: str | None = None
    source: dict[str, Any] | None = None


class QueryResponse(BaseModel):
    events: list[dict[str, Any]]
    result: str | None = None
    session_id: str | None = None


class CreateSessionRequest(AgentOptionsPayload):
    pass


class CreateSessionResponse(BaseModel):
    session_id: str
    status: str


class SendMessageRequest(BaseModel):
    message: str | None = None
    content: list[MessagePart] | None = None


class SessionInfo(BaseModel):
    session_id: str
    status: str
    queued_messages: int
    created_at: float | None = None
    updated_at: float | None = None


class CloseSessionResponse(BaseModel):
    session_id: str
    status: str


class CapabilitiesResponse(BaseModel):
    permission_modes: list[str]
    hook_events: list[str]
    endpoints: list[str]
    option_fields: list[str]
    supports: dict[str, bool]


class ValidationResponse(BaseModel):
    valid: bool
    normalized: dict[str, Any]
