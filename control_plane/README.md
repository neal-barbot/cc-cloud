# Sandbox Control Plane

This directory captures the multi-user isolation layer described in the source article. The production article assumes OSS/NAS plus a real sandbox scheduler; this repo includes a local filesystem implementation with the same lifecycle shape so the flow can be tested end to end.

## 生产化闭环清单（对应文章 1:1）

- `POST /users/{user_id}/query`：用户统一入口，按 user_id 路由到用户沙箱并转发 `v1/query`。
- `POST /users/{user_id}/query/stream`：用户统一 SSE 入口，按 user_id 路由 `v1/query/stream`。
- `/users/{user_id}/v1/{path}`：用户级通用代理，覆盖 capabilities、sessions、hooks、agents、mcp 等 sandbox 内 `/v1/...` 接口。
- `POST /control/allocate`：显式分配，支持用户复用、空闲池分配与动态创建。
- `POST /control/release`：控制面回收并触发快照持久化。
- `POST /users/{user_id}/release`：用户侧主动回收并持久化快照。
- `GET /control/assignments/{user_id}`：查询用户当前映射。
- `GET /control/sandboxes`：查看池中实例与状态。
- `POST /control/snapshots/{user_id}/compact`：将最新快照链压缩为新的全量快照。
- `POST /control/snapshots/{user_id}/prune`：删除不再被保留快照链引用的旧版本。

快照协议默认固定为两类路径（且回收/恢复链路只处理这两类）：

- `~/.claude/`
- `~/workspace/`

## Runtime Contract

1. Application requests include `user_id`.
2. The control plane maps `user_id -> sandbox endpoint`.
3. If no sandbox is active, the lifecycle manager allocates one from the idle pool.
4. The file-version manager restores the user's latest snapshot into:
   - `~/.claude/`
   - `~/workspace/`
5. Traffic is proxied to `http://sandbox:8765`.
6. On idle timeout or explicit release, the control plane snapshots changed files and frees the sandbox.

## Snapshot Layout

```text
snapshots/
  user_001/
    v1.tar.gz
    v2.tar.gz
    latest -> v2.tar.gz
```

The sandbox remains disposable. User memory, settings, sessions, MCP configuration, and workspace files live in versioned object storage.

## Minimal Proxy Flow

```text
POST /users/{user_id}/query
  -> lookup active sandbox
  -> allocate and restore snapshot if missing
  -> proxy POST /v1/query/stream to sandbox
  -> stream SSE back to caller
```

### Additional Control APIs

- `POST /users/{user_id}/query`：按用户调度沙箱并透传 `v1/query`（非流式）。
- `POST /users/{user_id}/query/stream`：按用户调度沙箱并透传 `v1/query/stream`（SSE）。
- `/users/{user_id}/v1/{path}`：通用透传 sandbox 的 `/v1/...` 接口，例如：
  - `GET /users/user_001/v1/capabilities`
  - `POST /users/user_001/v1/hooks/validate`
  - `POST /users/user_001/v1/agents/validate`
  - `POST /users/user_001/v1/mcp/validate`
  - `POST /users/user_001/v1/sessions/create`
  - `GET /users/user_001/v1/sessions/{session_id}/events`
- `POST /users/{user_id}/release`：用户侧主动回收沙箱并保存 `~/.claude` 与 `~/workspace`。
- `GET /control/sandboxes`：返回当前活跃映射。
- `POST /control/release`：支持 `snapshotPaths`（可选）；为空时默认回收 `~/.claude` 与 `workspace` 两条路径。
- `POST /control/snapshots/{user_id}/compact`：在清理旧版本前生成新的全量基线。
- `POST /control/snapshots/{user_id}/prune`：示例 body `{"keepLast":1}`，安全删除不再被最新链引用的旧版本。

## Local Control Plane Skeleton

Run it separately from the Claude HTTP service:

```bash
SNAPSHOT_ROOT=/tmp/claude-code-snapshots uvicorn control_plane.app:app --port 8766
```

Allocate a sandbox for a user:

```bash
curl -X POST http://localhost:8766/control/allocate \
  -H 'Content-Type: application/json' \
  -d '{"userId":"user_001","endpoint":"http://127.0.0.1:8765","restoreRoot":"/tmp/restore-user-001"}'
```

Release and snapshot user files:

```bash
curl -X POST http://localhost:8766/control/release \
  -H 'Content-Type: application/json' \
  -d '{"userId":"user_001","snapshotPaths":["/home/admin/.claude","/home/admin/workspace"]}'
```

The local store writes:

```text
/tmp/claude-code-snapshots/user_001/
  manifest.jsonl
  latest.tar.gz
  v1.tar.gz
  v2.tar.gz
```

## Production-aligned config

Use these environment variables to bind lifecycle + snapshot backend:

```bash
SNAPSHOT_ROOT=/tmp/claude-code-snapshots
SNAPSHOT_BACKEND_URI=file:///tmp/claude-code-snapshots         # switch to s3://bucket/prefix for OSS/NAS
SANDBOX_BACKEND=docker                                          # docker | k8s
SANDBOX_IMAGE=claude-code-http:latest
SANDBOX_POOL_SIZE=2                                             # precreate pool slots (optional)
SANDBOX_REMOVE_ON_RELEASE=false                                 # stop sandbox after release，set true to remove containers
SANDBOX_WAIT_FOR_HEALTH=false                                   # set true to block allocation until /healthz is ok
SANDBOX_HEALTH_CHECK_RETRIES=20
SANDBOX_HEALTH_CHECK_INTERVAL_SECONDS=0.75
SANDBOX_PURGE_ON_IDLE=false                                     # clear restore_root after idle snapshot
SANDBOX_DOCKER_ARGS="-e ANTHROPIC_API_KEY=..."                  # pass env to docker runtime
SANDBOX_IDLE_TIMEOUT_SECONDS=1800
SANDBOX_REAP_INTERVAL_SECONDS=30
```

### Local Replay Script

```bash
export SNAPSHOT_ROOT=/tmp/claude-code-snapshots
export SANDBOX_STATE_ROOT=/tmp/claude-sandboxes
export SANDBOX_POOL_SIZE=2
export SANDBOX_REMOVE_ON_RELEASE=false
export SANDBOX_IDLE_TIMEOUT_SECONDS=60

uvicorn control_plane.app:app --host 0.0.0.0 --port 8766

# 1) 分配并透传查询
curl -X POST http://127.0.0.1:8766/users/user_001/query \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"列出当前目录"}'

# 2) 流式查询
curl -N -X POST http://127.0.0.1:8766/users/user_001/query/stream \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"查看文件并给我总结"}'

# 3) 透传 sandbox 内其他 v1 API
curl http://127.0.0.1:8766/users/user_001/v1/capabilities

# 4) 主动回收（会保存 ~/.claude 与 workspace 快照）
curl -X POST http://127.0.0.1:8766/users/user_001/release

# 5) compact + prune 清理旧快照版本
curl -X POST http://127.0.0.1:8766/control/snapshots/user_001/compact
curl -X POST http://127.0.0.1:8766/control/snapshots/user_001/prune \
  -H 'Content-Type: application/json' \
  -d '{"keepLast":1}'

# 6) 2 分钟后空闲回收验证（或直接触发）
curl -X GET http://127.0.0.1:8766/control/sandboxes
```

### One-shot Closed-Loop Dry-run

Run this script to validate the full `users/{user_id}` proxy flow in one pass:

```bash
python scripts/control_plane_dry_run.py --backend all --user user_demo
python scripts/control_plane_dry_run.py --backend docker --user user_demo
python scripts/control_plane_dry_run.py --backend k8s --user user_demo
```

输出会包含：
- `/users/{user_id}/query` 分配、快照恢复、转发的闭环执行
- `/users/{user_id}/release` 触发快照持久化
- 回收后下一次 query 的 snapshot 复用链路
- 后台命令（docker/k8s）动作清单（用于对照文章中的调度行为）

### Real-Sandbox Smoke (真实容器闭环)

启动已有镜像并完成一套真实转发闭环：

```bash
export SANDBOX_IMAGE=claude-code-http:article-replica
export SANDBOX_DOCKER_ARGS='-e CLAUDE_HTTP_MOCK=true'
./scripts/verify_control_plane_sandbox.sh user_demo
```

脚本输出会帮助你确认：
- 分配拿到了 sandbox（`sandbox_id`）
- `/users/{user_id}/query` 转发成功
- `/users/{user_id}/v1/capabilities` 通用代理转发成功
- `release` 后 `manifest.jsonl` 记录版本
- 再次 query 时从 snapshot 复用恢复

### S3/OSS backend notes

For remote snapshot storage set:

```bash
SNAPSHOT_BACKEND_URI=s3://your-bucket/claude-snapshots
export AWS_REGION=cn-beijing
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export OSS_ACCESS_KEY_ID=...
export OSS_ACCESS_KEY_SECRET=...
export OSS_REGION=cn-beijing
export SNAPSHOT_OSS_ENDPOINT_URL=https://oss-cn-beijing.aliyuncs.com
```

If your object store uses a custom endpoint, set `SNAPSHOT_S3_ENDPOINT_URL`.
```
