#!/usr/bin/env bash
set -euo pipefail

USER_ID="${1:-user_demo}"
WORK_DIR="${2:-$(mktemp -d)}"
SNAPSHOT_ROOT="$WORK_DIR/snapshots"
STATE_ROOT="$WORK_DIR/state"
CONTROL_PLANE_PORT="${3:-8766}"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-claude-code-http:article-replica}"

mkdir -p "$SNAPSHOT_ROOT" "$STATE_ROOT"

export SNAPSHOT_ROOT
export SANDBOX_STATE_ROOT="$STATE_ROOT"
export SANDBOX_BACKEND="${SANDBOX_BACKEND:-docker}"
export SANDBOX_POOL_SIZE="${SANDBOX_POOL_SIZE:-1}"
export SANDBOX_IMAGE
export SANDBOX_DOCKER_ARGS="${SANDBOX_DOCKER_ARGS:--e CLAUDE_HTTP_MOCK=true}"
export SANDBOX_WAIT_FOR_HEALTH="${SANDBOX_WAIT_FOR_HEALTH:-true}"
export SNAPSHOT_BACKEND_URI="${SNAPSHOT_BACKEND_URI:-file://$SNAPSHOT_ROOT}"
export SANDBOX_REMOVE_ON_RELEASE="${SANDBOX_REMOVE_ON_RELEASE:-false}"
export SANDBOX_IDLE_TIMEOUT_SECONDS="${SANDBOX_IDLE_TIMEOUT_SECONDS:-9999}"
export SANDBOX_REAP_INTERVAL_SECONDS="${SANDBOX_REAP_INTERVAL_SECONDS:-5}"

CONTROL_PLANE_LOG="$WORK_DIR/control_plane.log"
CONTROL_PID=""

cleanup() {
  if [[ -n "${CONTROL_PID}" ]]; then
    kill "$CONTROL_PID" >/dev/null 2>&1 || true
    wait "$CONTROL_PID" 2>/dev/null || true
  fi
  docker ps --filter name=cp-sandbox- --filter status=running -q | xargs -r docker rm -f >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$(cd "$(dirname "$0")/.." && pwd)"
python -m uvicorn control_plane.app:app --host 127.0.0.1 --port "$CONTROL_PLANE_PORT" > "$CONTROL_PLANE_LOG" 2>&1 &
CONTROL_PID=$!

for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${CONTROL_PLANE_PORT}/control/sandboxes" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
  if [[ $i -eq 30 ]]; then
    echo "Control plane startup timeout"
    tail -n 120 "$CONTROL_PLANE_LOG"
    exit 1
  fi
done

echo "==> 第1轮：触发分配与恢复"
curl -sS -X POST "http://127.0.0.1:${CONTROL_PLANE_PORT}/users/${USER_ID}/query" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"第一轮：初始化并建立快照"}' >/dev/null

echo "==> 验证用户级通用 v1 代理"
curl -sS "http://127.0.0.1:${CONTROL_PLANE_PORT}/users/${USER_ID}/v1/capabilities" >/dev/null

SANDBOX_ID="$(python - <<PY
import json
from pathlib import Path

state = json.loads(Path("$STATE_ROOT/control_state.json").read_text(encoding="utf-8"))
print(state["assignments"]["$USER_ID"]["sandbox_id"])
PY
)"
echo "Assigned sandbox: ${SANDBOX_ID}"

USER_ROOT="${STATE_ROOT}/${SANDBOX_ID}"
mkdir -p "${USER_ROOT}/workspace" "${USER_ROOT}/.claude"
printf 'hello from control-plane smoke test' > "${USER_ROOT}/workspace/verify.txt"
printf '{\"hello\":\"world\"}' > "${USER_ROOT}/.claude/settings.json"

echo "==> 第2轮：主动 release 写入快照"
curl -sS -X POST "http://127.0.0.1:${CONTROL_PLANE_PORT}/users/${USER_ID}/release" >/dev/null

echo "==> 第3轮：再次发起查询，验证恢复并复用"
curl -sS -X POST "http://127.0.0.1:${CONTROL_PLANE_PORT}/users/${USER_ID}/query" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"第二轮：验证恢复可见"}' >/dev/null

echo "==> Snapshot versions:"
python - <<PY
from pathlib import Path
import json

manifest = Path("$SNAPSHOT_ROOT/$USER_ID/manifest.jsonl")
print("manifest exists:", manifest.exists())
if manifest.exists():
    versions = [json.loads(line)["version"] for line in manifest.read_text().splitlines() if line.strip()]
    print("versions:", versions)
    print("file count latest:", json.loads(manifest.read_text().splitlines()[-1])["file_count"])
PY

echo "==> Done. Logs: $CONTROL_PLANE_LOG"
