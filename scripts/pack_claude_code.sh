#!/usr/bin/env bash
set -euo pipefail

out_dir="${1:-dist}"
mkdir -p "$out_dir"

echo "[INFO] Packing @anthropic-ai/claude-code for offline install ..."
npm pack @anthropic-ai/claude-code --pack-destination "$out_dir"
ls -lh "$out_dir"/anthropic-ai-claude-code-*.tgz
