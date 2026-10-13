#!/usr/bin/env bash
# On-demand uzu lane for Qwen3.8-27B (tree speculative decoding).
# Smarter than the 9B but bandwidth-capped (~24 t/s). ~15GB.
# Serves an OpenAI-compatible API so any tool can use it as a lane.
#
# Usage: uzu-27b-lane {start|stop|status}
#
# Override via env:
#   UZU27B_PORT  UZU_CLI  UZU27B_MODEL  UZU27B_LOG
set -euo pipefail

PORT="${UZU27B_PORT:-8094}"
UZU_CLI="${UZU_CLI:-$HOME/projects/uzu/target/release/cli}"
UZU_MODEL="${UZU27B_MODEL:-qwen3.8:27b}"
LOG="${UZU27B_LOG:-$HOME/.hermes/logs/uzu-27b-lane.log}"

is_up() { curl -sf --max-time 3 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; }

case "${1:-}" in
  start)
    if is_up; then echo "already up on :${PORT}"; exit 0; fi
    mkdir -p "$(dirname "$LOG")"
    FREE=$( { memory_pressure 2>/dev/null || true; } | awk '/free percentage/ {gsub("%","",$NF); print $NF}')
    if [ -n "${FREE:-}" ] && [ "$FREE" -lt 45 ]; then
      echo "WARNING: only ${FREE}% memory headroom — uzu 27B needs ~15GB. Free RAM first (stop other lanes)." >&2
    fi
    exec env UZU_ALWAYS_INLINE=1 "$UZU_CLI" server --model "$UZU_MODEL" --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1
    ;;
  stop)
    pkill -f "uzu .*server .*--port[= ]${PORT}([^0-9]|$)" 2>/dev/null && echo "stopped" || echo "not running"
    ;;
  status)
    if is_up; then echo "UP on :${PORT}"; else echo "DOWN"; fi
    ;;
  *)
    echo "usage: uzu-27b-lane {start|stop|status}" >&2; exit 1;;
esac
