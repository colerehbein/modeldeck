#!/usr/bin/env bash
# On-demand MiniCPM-V 4.7 35B-A3B lane (uncensored, vision, llama.cpp).
# Refusal direction ablated (AtomicChat AD build): answers what the original
# declines. MoE (~3B active) so generation is fast for its size; ~20GB on disk.
# Usage: minicpm-v-lane {start|stop|status}
#
# Override via env:
#   MINICPM_PORT  MODEL_GGUF  MMPROJ_GGUF  LLAMA_SERVER  CTX_SIZE  MINICPM_LOG
#
# Notes:
# - --jinja loads the model's own chat template, which starts answers in
#   THINKING mode by default. Per request, turn it off with
#   "chat_template_kwargs": {"enable_thinking": false}.
# - Q8 KV cache is mandatory on a 32GB box: FP16 KV at this context pushes
#   weights+KV past unified memory and Metal dies mid-decode (the 35B lesson).
set -euo pipefail

PORT="${MINICPM_PORT:-8093}"
MODEL="${MODEL_GGUF:-$HOME/.lmstudio/models/AtomicChat/MiniCPM-V-4.7-35B-A3B-Uncensored-GGUF/MiniCPM-V-4.7-35B-A3B-Uncensored-AD-Q4_K-IQ4_XS/MiniCPM-V-4.7-35B-A3B-Uncensored-AD-Q4_K-IQ4_XS-00001-of-00011.gguf}"
MMPROJ="${MMPROJ_GGUF:-$HOME/.lmstudio/models/AtomicChat/MiniCPM-V-4.7-35B-A3B-Uncensored-GGUF/mmproj-MiniCPM-V-4.7-35B-A3B-Uncensored-F16.gguf}"
LLAMA="${LLAMA_SERVER:-/opt/homebrew/bin/llama-server}"
CTX="${CTX_SIZE:-32768}"
LOG="${MINICPM_LOG:-$HOME/.hermes/logs/minicpm-v-lane.log}"

is_up() { curl -sf --max-time 3 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; }

case "${1:-}" in
  start)
    if is_up; then echo "already up on :${PORT}"; exit 0; fi
    mkdir -p "$(dirname "$LOG")"
    FREE=$( { memory_pressure 2>/dev/null || true; } | awk '/free percentage/ {gsub("%","",$NF); print $NF}')
    if [ -n "${FREE:-}" ] && [ "$FREE" -lt 40 ]; then
      echo "WARNING: only ${FREE}% memory headroom — this model needs ~20GB. Free RAM first (a reboot clears swap debt) or it will thrash." >&2
    fi
    exec "$LLAMA" \
      --model "$MODEL" \
      --mmproj "$MMPROJ" \
      --host 127.0.0.1 --port "$PORT" \
      --ctx-size "$CTX" -ngl 999 --jinja --parallel 1 \
      --cache-type-k q8_0 --cache-type-v q8_0 \
      >>"$LOG" 2>&1
    ;;
  stop)
    pkill -f "llama-server .*--port[= ]${PORT}([^0-9]|$)" 2>/dev/null && echo "stopped" || echo "not running"
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
    echo "usage: minicpm-v-lane {start|stop|status}" >&2; exit 1;;
esac
