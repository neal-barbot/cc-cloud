from __future__ import annotations

import asyncio
import inspect
import os
import re
from collections.abc import AsyncGenerator
from typing import Any

from app.models.schemas import AgentOptionsPayload, HookSpec, QueryRequest

try:
    from claude_agent_sdk import (  # type: ignore
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        HookMatcher,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        query,
    )
except Exception:  # pragma: no cover - exercised when SDK is not installed.
    AssistantMessage = None
    ClaudeAgentOptions = None
    ClaudeSDKClient = None
    HookMatcher = None
    ResultMessage = None
    SystemMessage = None
    TextBlock = None
    ToolResultBlock = None
    ToolUseBlock = None
    query = None

DEFAULT_PERMISSION_MODE = "bypassPermissions"


def sdk_available() -> bool:
    return ClaudeAgentOptions is not None and query is not None and ClaudeSDKClient is not None


def use_mock_backend() -> bool:
    return os.getenv("CLAUDE_HTTP_MOCK", "false").lower() in {"1", "true", "yes"}


def _model_dump(payload: AgentOptionsPayload) -> dict[str, Any]:
    return payload.model_dump(by_alias=False, exclude_none=True)


def _constructor_kwargs(cls: type[Any], values: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(cls)
    except (TypeError, ValueError):
        return values
    allowed = set(signature.parameters)
    return {key: value for key, value in values.items() if key in allowed}


def _build_hooks(hooks: dict[str, list[HookSpec]]) -> dict[str, list[Any]]:
    if not hooks or HookMatcher is None:
        return {}

    built: dict[str, list[Any]] = {}
    for event_name, specs in hooks.items():
        matchers: list[Any] = []
        for spec in specs:
            async def hook(input_data: dict[str, Any], tool_use_id: str | None = None, context: Any = None, *, _spec: HookSpec = spec, _event_name: str = event_name) -> dict[str, Any]:
                tool_name = _extract_tool_name(input_data)
                if _spec.matcher and tool_name and not re.search(_spec.matcher, tool_name):
                    return {}
                if _spec.action == "deny":
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": _event_name,
                            "permissionDecision": "deny",
                            "permissionDecisionReason": _spec.reason or "Denied by HTTP hook policy.",
                        }
                    }
                if _spec.action == "webhook":
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": _event_name,
                            "additionalContext": f"Webhook configured: {_spec.webhook_url}",
                        }
                    }
                return {}

            matchers.append(HookMatcher(matcher=spec.matcher, hooks=[hook], timeout=spec.timeout))
        built[event_name] = matchers
    return built


def _extract_tool_name(input_data: dict[str, Any]) -> str | None:
    if not isinstance(input_data, dict):
        return None
    return (
        input_data.get("tool_name")
        or input_data.get("toolName")
        or input_data.get("name")
        or (input_data.get("tool_input") or {}).get("name")
    )


def build_options(payload: AgentOptionsPayload) -> Any:
    if ClaudeAgentOptions is None:
        return None

    raw = _model_dump(payload)
    values: dict[str, Any] = {
        "tools": raw.get("tools") or None,
        "system_prompt": raw.get("system_prompt"),
        "cwd": raw.get("cwd"),
        "max_turns": raw.get("max_turns"),
        "max_budget_usd": raw.get("max_budget_usd"),
        "allowed_tools": raw.get("allowed_tools") or None,
        "disallowed_tools": raw.get("disallowed_tools") or None,
        "permission_mode": raw.get("permission_mode") or DEFAULT_PERMISSION_MODE,
        "mcp_servers": raw.get("mcp_servers") or None,
        "hooks": _build_hooks(payload.hooks) or None,
        "skills": raw.get("skills"),
        "plugins": raw.get("plugins") or None,
        "model": raw.get("model"),
        "fallback_model": raw.get("fallback_model"),
        "include_partial_messages": raw.get("include_partial_messages"),
        "include_hook_events": raw.get("include_hook_events"),
        "continue_conversation": raw.get("continue_conversation"),
        "resume": raw.get("resume"),
        "session_id": raw.get("session_id"),
        "add_dirs": raw.get("add_dirs") or None,
        "env": raw.get("env") or None,
        "extra_args": raw.get("extra_args") or None,
    }
    if payload.agents:
        values["agents"] = {name: agent.model_dump(exclude_none=True) for name, agent in payload.agents.items()}
    values.update(payload.extra)
    values = {key: value for key, value in values.items() if value not in (None, [], {})}
    return ClaudeAgentOptions(**_constructor_kwargs(ClaudeAgentOptions, values))


def normalize_payload(payload: AgentOptionsPayload) -> dict[str, Any]:
    raw = payload.model_dump(by_alias=True, exclude_none=True)
    raw["permissionMode"] = raw.get("permissionMode") or DEFAULT_PERMISSION_MODE
    return raw


def serialize_message(message: Any) -> dict[str, Any]:
    if SystemMessage is not None and isinstance(message, SystemMessage):
        data = getattr(message, "data", {}) or {}
        return {
            "event": "system",
            "data": {
                "subtype": getattr(message, "subtype", data.get("subtype", None)),
                "session_id": getattr(message, "session_id", None) or data.get("session_id"),
                "details": data,
            },
        }

    if AssistantMessage is not None and isinstance(message, AssistantMessage):
        return {
            "event": "assistant",
            "data": {
                "content": [_serialize_block(block) for block in getattr(message, "content", [])],
                "parent_tool_use_id": getattr(message, "parent_tool_use_id", None),
            },
        }

    if ResultMessage is not None and isinstance(message, ResultMessage):
        return {
            "event": "result",
            "data": {
                "subtype": getattr(message, "subtype", None),
                "result": getattr(message, "result", None),
                "session_id": getattr(message, "session_id", None),
                "total_cost_usd": getattr(message, "total_cost_usd", None),
                "duration_ms": getattr(message, "duration_ms", None),
                "is_error": getattr(message, "is_error", None),
            },
        }

    if isinstance(message, dict) and "event" in message:
        return message

    message_type = type(message).__name__
    data = _safe_object_dict(message)
    if message_type.endswith("Event") or message_type.endswith("Message"):
        event_name = getattr(message, "type", None) or message_type
        return {"event": str(event_name), "data": data or {"raw": str(message)}}

    return {"event": "message", "data": data or {"raw": str(message)}}


def _serialize_block(block: Any) -> dict[str, Any]:
    if TextBlock is not None and isinstance(block, TextBlock):
        return {"type": "text", "text": getattr(block, "text", "")}
    if ToolUseBlock is not None and isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "name": getattr(block, "name", None),
            "id": getattr(block, "id", None),
            "input": getattr(block, "input", {}),
        }
    if ToolResultBlock is not None and isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "tool_use_id", None),
            "content": getattr(block, "content", None),
            "is_error": getattr(block, "is_error", None),
        }
    if isinstance(block, dict):
        return block
    return {"type": getattr(block, "type", "unknown"), "raw": str(block)}


def _safe_object_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json", exclude_none=True)
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        result: dict[str, Any] = {}
        for key, item in vars(value).items():
            if key.startswith("_"):
                continue
            try:
                json_ready = item if isinstance(item, (str, int, float, bool, type(None), list, dict)) else str(item)
            except Exception:
                json_ready = repr(item)
            result[key] = json_ready
        return result
    return {}


async def run_query_stream(req: QueryRequest) -> AsyncGenerator[dict[str, Any], None]:
    if use_mock_backend() or not sdk_available():
        async for event in _mock_query_stream(req.prompt):
            yield event
        return

    options = build_options(req)
    async for message in query(prompt=req.prompt, options=options):
        yield serialize_message(message)


async def run_query(req: QueryRequest) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async for event in run_query_stream(req):
        events.append(event)
    return events


async def _mock_query_stream(prompt: str) -> AsyncGenerator[dict[str, Any], None]:
    session_id = f"mock-{abs(hash(prompt)) % 1_000_000}"
    yield {"event": "system", "data": {"subtype": "init", "session_id": session_id, "details": {"backend": "mock"}}}
    await asyncio.sleep(0)
    yield {
        "event": "assistant",
        "data": {"content": [{"type": "text", "text": f"Mock Claude received: {prompt}"}], "parent_tool_use_id": None},
    }
    await asyncio.sleep(0)
    yield {
        "event": "result",
        "data": {"subtype": "success", "result": f"Mock result for: {prompt}", "session_id": session_id, "total_cost_usd": 0.0},
    }


CAPABILITIES = {
    "permission_modes": ["default", "dontAsk", "acceptEdits", "bypassPermissions", "plan", "auto"],
    "hook_events": [
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
    ],
    "endpoints": [
        "GET /healthz",
        "GET /readyz",
        "GET /v1/capabilities",
        "POST /v1/query",
        "POST /v1/query/stream",
        "POST /v1/sessions/create",
        "GET /v1/sessions",
        "GET /v1/sessions/{session_id}",
        "POST /v1/sessions/{session_id}/send",
        "GET /v1/sessions/{session_id}/events",
        "DELETE /v1/sessions/{session_id}",
        "POST /v1/hooks/validate",
        "POST /v1/agents/validate",
        "POST /v1/mcp/validate",
    ],
    "option_fields": list(AgentOptionsPayload.model_fields),
    "supports": {
        "single_shot_query": True,
        "sse_streaming": True,
        "streaming_input_sessions": True,
        "hooks": True,
        "subagents": True,
        "mcp_servers": True,
        "skills": True,
        "plugins": True,
        "image_messages": True,
        "mock_backend": True,
    },
}
