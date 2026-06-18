# Claude Code HTTP Replica Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recreate the project described in the pasted spec: a FastAPI + SSE HTTP service around Claude Agent SDK, deployment assets, and sandbox isolation design.

**Architecture:** The app exposes `/v1/query`, `/v1/query/stream`, and `/v1/sessions/*` endpoints backed by a small service layer that can run against the real `claude-agent-sdk` or a deterministic mock backend. Deployment files mirror the article's offline install and sandbox image story, while docs explain the user-snapshot control plane.

**Tech Stack:** Python 3.11+, FastAPI, sse-starlette, Pydantic v2, claude-agent-sdk, pytest, httpx.

---

### Task 1: Project Skeleton

**Files:**
- Create: `app/__init__.py`
- Create: `app/main.py`
- Create: `app/routers/__init__.py`
- Create: `app/routers/health.py`
- Create: `run.py`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`

- [x] Create package directories and minimal FastAPI app.
- [x] Wire `/healthz`, `/readyz`, and `/v1` routers.
- [x] Add dependency files for runtime and tests.

### Task 2: Query and Session Service

**Files:**
- Create: `app/models/schemas.py`
- Create: `app/services/agent_service.py`
- Create: `app/services/session_service.py`
- Create: `app/routers/query.py`
- Create: `app/routers/sessions.py`

- [x] Define Pydantic request/response models for query, session, hooks, MCP servers, and subagents.
- [x] Implement SDK option construction with `bypassPermissions` as unattended default.
- [x] Serialize SDK messages into stable SSE dictionaries.
- [x] Implement deterministic mock backend for local verification.
- [x] Implement streaming sessions with response queues and heartbeats.

### Task 3: Deployment and Control Plane Materials

**Files:**
- Create: `Dockerfile`
- Create: `docker/sandbox_start.sh`
- Create: `scripts/pack_claude_code.sh`
- Create: `scripts/offline_install.sh`
- Create: `control_plane/README.md`
- Create: `control_plane/schema.sql`

- [x] Add a Docker image that installs Python dependencies and can install Claude Code from npm or local tgz.
- [x] Add offline packaging/install helper scripts.
- [x] Document one-user-one-sandbox lifecycle and snapshot storage schema.

### Task 4: Tests and Verification

**Files:**
- Create: `tests/test_app.py`
- Create: `pytest.ini`
- Create: `README.md`

- [x] Test health, sync query, streaming query, and session lifecycle in mock mode.
- [x] Add runnable README with curl examples.
- [x] Run `python -m pytest`.
