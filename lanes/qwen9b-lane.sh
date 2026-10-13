#!/usr/bin/env bash
# On-demand Qwen3.5-9B local lane (long-context, llama.cpp).
# Loads ~6.5GB at 128K context.
#
# Usage: qwen9b-lane {start|stop|status}
#
# Override via env:
#   QWEN9B_PORT  MODEL_GGUF  LLAMA_SERVER  QWEN9B_LOG
set -euo pipefail

PORT="${QWEN9B_PORT:-8097}"
MODEL="${MODEL_GGUF:-$HOME/.lmstudio/models/DavidAU/Qwen3.5-9B-Defiant-Fable-GGUF/Qwen3.5-9B-The-Defiant-Fable-Uncnr-Heretic-NEO-MAX-Q4_K_M.gguf}"
LLAMA="${LLAMA_SERVER:-/opt/homebrew/bin/llama-server}"
LOG="${QWEN9B_LOG:-$HOME/.hermes/logs/qwen9b-lane.log}"

is_up() { curl -sf --max-time 3 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; }

case "${1:-}" in
  start)
    if is_up; then echo "already up on :${PORT}"; exit 0; fi
    mkdir -p "$(dirname "$LOG")"
    FREE=$( { memory_pressure 2>/dev/null || true; } | awk '/free percentage/ {gsub("%","",$NF); print $NF}')
    if [ -n "${FREE:-}" ] && [ "$FREE" -lt 20 ]; then
      echo "WARNING: only ${FREE}% memory headroom — loading may be tight. Free some RAM first." >&2
    fi
    exec "$LLAMA" \
      --model "$MODEL" \
      --host 127.0.0.1 --port "$PORT" \
      --ctx-size 131072 -ngl 999 --jinja --parallel 1 \
      --cache-type-k q8_0 --cache-type-v q8_0 \
      >>"$LOG" 2>&1
    ;;
  stop)
    PAT="llama-server .*--port[= ]${PORT}([^0-9]|$)"
    # Graceful: SIGTERM first, give the server ~3s to close active
    # connections (an in-flight stream survives), then SIGKILL whatever
    # is still alive. The anchored pattern is unchanged.
    pkill -TERM -f "$PAT" 2>/dev/null || { echo "not running"; exit 0; }
    for _ in 1 2 3 4 5 6; do
      pgrep -f "$PAT" >/dev/null 2>&1 || break
      sleep 0.5
    done
    pkill -KILL -f "$PAT" 2>/dev/null || true
    echo "stopped"
    ;;
  status)
    if is_up; then echo "UP on :${PORT}"; else echo "DOWN"; fi
    ;;
  *)
    echo "usage: qwen9b-lane {start|stop|status}" >&2; exit 1;;
esac
