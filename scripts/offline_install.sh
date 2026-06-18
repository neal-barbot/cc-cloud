#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 /path/to/anthropic-ai-claude-code-*.tgz [node-tarball.tar.xz]" >&2
  exit 64
fi

claude_tgz="$1"
node_tarball="${2:-}"

if [ -n "$node_tarball" ]; then
  install_dir="${HOME}/.local/node-offline"
  mkdir -p "$install_dir"
  tar -xf "$node_tarball" -C "$install_dir" --strip-components=1
  export PATH="$install_dir/bin:$PATH"
fi

node -v
npm -v
npm install -g "$claude_tgz"
claude --version
