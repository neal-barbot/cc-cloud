# Claude Code HTTP Service Replica

这是对贴文方案的 1:1 工程化复现：把 Claude Code / Claude Agent SDK 封装成 FastAPI HTTP 服务，支持同步查询、SSE 流式查询、多轮 session、hooks、MCP、subagents、skills、plugins 配置透传，以及离线部署和一用户一沙箱隔离材料。

## Article Mapping

| 文章模块 | 本仓库对应 |
| --- | --- |
| 云端离线部署 | `scripts/pack_claude_code.sh`、`scripts/offline_install.sh` |
| FastAPI + SSE 服务化 | `app/routers/query.py`、`app/services/agent_service.py` |
| `query()` 单次查询 | `POST /v1/query`、`POST /v1/query/stream` |
| `ClaudeSDKClient` 多轮会话 | `POST /v1/sessions/create`、`/send`、`/events` |
| Permission / Hooks / Subagents / MCP | `POST /v1/hooks/validate`、`/agents/validate`、`/mcp/validate` 以及 query/session payload |
| 基础镜像 | `Dockerfile`、`docker/sandbox_start.sh` |
| 一用户一沙箱 + 文件版本化 | `control_plane/app.py`、`control_plane/snapshot_store.py`、`control_plane/schema.sql` |

## Run Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
CLAUDE_HTTP_MOCK=true python run.py
```

如果要使用真实 Claude-compatible 后端，先复制环境变量模板，再把密钥填到本机 `.env`：

```bash
cp .env.example .env
# edit .env, keep CLAUDE_HTTP_MOCK=false, fill ANTHROPIC_AUTH_TOKEN
python run.py
```

注意：`.env` 已被 `.gitignore` 排除，不要把真实 token 写进 README、Dockerfile 或代码。

打开 API 文档：

```text
http://localhost:8765/docs
```

## Query API

```bash
curl -X POST http://localhost:8765/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"列出当前目录的文件"}'
```

```bash
curl -N -X POST http://localhost:8765/v1/query/stream \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"分析当前目录下的代码结构"}'
```

## Session API

```bash
SESSION=$(curl -s -X POST http://localhost:8765/v1/sessions/create \
  -H 'Content-Type: application/json' \
  -d '{"allowedTools":["Read","Edit","Glob"]}' | jq -r '.session_id')

curl -N http://localhost:8765/v1/sessions/$SESSION/events

curl -X POST http://localhost:8765/v1/sessions/$SESSION/send \
  -H 'Content-Type: application/json' \
  -d '{"message":"分析 auth 模块"}'
```

## Advanced Payload

```json
{
  "prompt": "审查并优化代码",
  "permissionMode": "bypassPermissions",
  "allowedTools": ["Read", "Edit", "Glob"],
  "agents": {
    "code-reviewer": {
      "description": "代码审查专家",
      "prompt": "关注安全性和最佳实践",
      "tools": ["Read", "Glob", "Grep"]
    }
  },
  "mcpServers": {
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "ghp_xxx"}
    }
  },
  "hooks": {
    "PreToolUse": [
      {"matcher": "Write|Edit", "action": "deny", "reason": "禁止写入 .env 文件"}
    ]
  }
}
```

## Configuration APIs

这些接口用于在 `/docs` 里明确暴露文章提到的远程配置面：

```bash
curl http://localhost:8765/v1/capabilities

curl -X POST http://localhost:8765/v1/hooks/validate \
  -H 'Content-Type: application/json' \
  -d '{"hooks":{"PreToolUse":[{"matcher":"Write|Edit","action":"deny","reason":"禁止写入 .env 文件"}]}}'

curl -X POST http://localhost:8765/v1/agents/validate \
  -H 'Content-Type: application/json' \
  -d '{"agents":{"code-reviewer":{"prompt":"关注安全性和最佳实践","tools":["Read","Glob","Grep"]}}}'

curl -X POST http://localhost:8765/v1/mcp/validate \
  -H 'Content-Type: application/json' \
  -d '{"mcpServers":{"github":{"type":"stdio","command":"npx","args":["-y","@modelcontextprotocol/server-github"]}}}'
```

图片消息可以通过 session `content` 发送：

```json
{
  "content": [
    {"type": "text", "text": "分析这张图"},
    {
      "type": "image",
      "source": {
        "type": "base64",
        "media_type": "image/png",
        "data": "..."
      }
    }
  ]
}
```

## Real Claude Mode

默认会调用真实 `claude-agent-sdk`。本地没有认证或只想验证接口时，设置：

```bash
export CLAUDE_HTTP_MOCK=true
```

你可以通过 `.env` 或容器环境变量传入兼容 Anthropic 协议的网关配置，例如：

```bash
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-v4-pro[1m]
ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
ENABLE_TOOL_SEARCH=true
```

官方 SDK 当前说明：`query()` 返回异步消息迭代器，`ClaudeSDKClient` 支持双向会话，Python 包会自带 Claude Code CLI；本项目仍保留 `scripts/pack_claude_code.sh` 与 `scripts/offline_install.sh` 来覆盖文章中的离线 npm 包部署路径。

## Docker

```bash
docker build -t claude-code-http .
docker run --rm -p 8765:8765 -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" claude-code-http
```

如果目标环境无外网：

```bash
./scripts/pack_claude_code.sh dist
./scripts/offline_install.sh dist/anthropic-ai-claude-code-*.tgz
```

## Sandbox Isolation

`control_plane/` 里包含一用户一沙箱、用户文件版本化、快照表结构和生命周期说明。实际生产环境中由控制面负责分配沙箱、恢复 `~/.claude/` 与 `~/workspace/` 快照、代理 HTTP/SSE 请求，并在回收时生成新版本。

本地可运行骨架：

```bash
SNAPSHOT_ROOT=/tmp/claude-code-snapshots uvicorn control_plane.app:app --port 8766
```

新增可复现「用户侧代理」入口：

```bash
POST /users/{user_id}/query
POST /users/{user_id}/query/stream
POST /users/{user_id}/release
```

控制面额外环境变量（示例）：

```bash
SANDBOX_BACKEND=docker # docker | k8s
SANDBOX_IMAGE=claude-code-http:latest
SNAPSHOT_BACKEND_URI=file:///tmp/claude-code-snapshots
SANDBOX_STATE_ROOT=/tmp/claude-sandbox-states
SANDBOX_POOL_SIZE=2                                             # 按需预热池（可选）
SANDBOX_REMOVE_ON_RELEASE=true                                  # 回收时停止/销毁容器
SANDBOX_DOCKER_ARGS="-e ANTHROPIC_API_KEY=..."                  # 向沙箱注入运行时环境
SANDBOX_IDLE_TIMEOUT_SECONDS=1800
SANDBOX_REAP_INTERVAL_SECONDS=30
```
