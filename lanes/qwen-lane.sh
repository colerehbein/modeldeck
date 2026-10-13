#!/usr/bin/env bash
# On-demand Qwen3.6-35B-A3B local lane (uncensored + vision, llama.cpp).
# Loads ~20GB only when you run this.
#
# Usage: qwen-lane {start|stop|status}
#
# Override any value via env:
#   QWEN35B_PORT  MODEL_GGUF  MMPROJ_GGUF  LLAMA_SERVER  QWEN35B_LOG
set -euo pipefail

PORT="${QWEN35B_PORT:-8099}"
MODEL="${MODEL_GGUF:-$HOME/.lmstudio/models/LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-Hermes-Final-GGUF/Hermes3.6-35B-A3B-Uncensored-Genesis-V13-MTP-APEX-Compact.gguf}"
MMPROJ="${MMPROJ_GGUF:-$HOME/.lmstudio/models/LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-Hermes-Final-GGUF/mmproj-Hermes3.6-35B-A3B-Uncensored-Genesis-F16.gguf}"
LLAMA="${LLAMA_SERVER:-/opt/homebrew/bin/llama-server}"
LOG="${QWEN35B_LOG:-$HOME/.hermes/logs/qwen-lane.log}"

is_up() { curl -sf --max-time 3 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; }

case "${1:-}" in
  start)
    if is_up; then echo "already up on :${PORT}"; exit 0; fi
    # memory guard: refuse to load if the box is already tight
    FREE=$(memory_pressure 2>/dev/null | awk '/free percentage/ {gsub("%","",$NF); print 100-$NF}')
    if [ -n "${FREE:-}" ] && [ "$FREE" -lt 25 ]; then
      echo "WARNING: only ${FREE}% memory headroom — loading 20GB may thrash. Free some RAM first." >&2
    fi
    exec "$LLAMA" \
      --model "$MODEL" \
      --mmproj "$MMPROJ" \
      --host 127.0.0.1 --port "$PORT" \
      --ctx-size 65536 -ngl 999 --jinja --parallel 1 \
      --cache-type-k q8_0 --cache-type-v q8_0 \
      >>"$LOG" 2>&1
    ;;
  stop)
    pkill -f "llama-server.*${PORT}" 2>/dev/null && echo "stopped" || echo "not running"
    ;;
  status)
    if is_up; then
      echo "UP on :${PORT}"
      curl -s "http://127.0.0.1:${PORT}/props" 2>/dev/null | python3 -c "import json,sys; print('  ctx:', json.load(sys.stdin).get('n_ctx','?'))" 2>/dev/null
    else
      echo "DOWN"
    fi
    ;;
  *)
    echo "usage: qwen-lane {start|stop|status}" >&2; exit 1;;
esac
