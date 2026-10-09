#!/usr/bin/env bash
# Run one Qwen 3.8 Flash SGLang server within an existing Slurm allocation.
#
# This is intentionally an artifact of the_lab rather than a reference to a
# checkout on the submit host: the executor copies this exact launcher into
# every job directory.  We do, however, reuse the operator-managed HF cache
# under MODEL_INFERENCE_ROOT, so weights are never copied per experiment.

set -euo pipefail

: "${MODEL_INFERENCE_ROOT:=/home/davidh/architects/model_inference}"
: "${SGLANG_PORT:=30000}"
: "${SGLANG_READY_TIMEOUT_S:=2700}"
: "${THE_LAB_MODEL_READY_FILE:?THE_LAB_MODEL_READY_FILE is required}"

MODEL="${MODEL:-RadixArk/Qwen3.8-Flash-Next-NVFP4}"
MODEL_NAME="${MODEL_NAME:-qwen3.8-flash-next}"
GPU_MEMORY_FRACTION="${GPU_MEMORY_FRACTION:-0.50}"
CONTEXT_LENGTH="${CONTEXT_LENGTH:-262144}"
MAX_RUNNING_REQUESTS="${MAX_RUNNING_REQUESTS:-1}"
CHUNKED_PREFILL_SIZE="${CHUNKED_PREFILL_SIZE:-2048}"
DECODE_GRAPH_MAX_BS="${DECODE_GRAPH_MAX_BS:-$MAX_RUNNING_REQUESTS}"
GPU_MEMORY_GUARD_SCRIPT="${GPU_MEMORY_GUARD_SCRIPT:-}"
INFERENCE_CAPTURE_SCRIPT="${INFERENCE_CAPTURE_SCRIPT:-}"
INFERENCE_CAPTURE_DIR="$(dirname "$THE_LAB_MODEL_READY_FILE")"
export INFERENCE_CAPTURE_SCRIPT INFERENCE_CAPTURE_DIR
if [ -n "$INFERENCE_CAPTURE_SCRIPT" ] && [ ! -r "$INFERENCE_CAPTURE_SCRIPT" ]; then
  echo "Inference capture script is not readable" >&2; exit 1
fi
MAMBA_CACHE_SIZE="${MAMBA_CACHE_SIZE:-}"
DISABLE_RADIX_CACHE="${DISABLE_RADIX_CACHE:-0}"
case "$DISABLE_RADIX_CACHE" in 0|1) ;; *) echo "invalid DISABLE_RADIX_CACHE" >&2; exit 1 ;; esac
SPECULATIVE_STEPS="${SPECULATIVE_STEPS:-0}"
[[ "$SPECULATIVE_STEPS" =~ ^[0-9]+$ ]] || { echo "invalid SPECULATIVE_STEPS" >&2; exit 1; }
SPECULATIVE_DRAFT_QUANTIZATION="${SPECULATIVE_DRAFT_QUANTIZATION:-}"
case "$SPECULATIVE_DRAFT_QUANTIZATION" in
  ""|unquant|nvfp4_online) ;;
  *) echo "invalid SPECULATIVE_DRAFT_QUANTIZATION (unquant or nvfp4_online)" >&2; exit 1 ;;
esac
export MAMBA_CACHE_SIZE DISABLE_RADIX_CACHE SPECULATIVE_STEPS SPECULATIVE_DRAFT_QUANTIZATION
# --- serving-m2 levers (Kaggle milestone-2 notebook port).  Every var below
# defaults to unset, so leaving them unset reproduces the previous command line.
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-}"
case "$KV_CACHE_DTYPE" in ""|auto|fp8_e4m3) ;; *) echo "invalid KV_CACHE_DTYPE" >&2; exit 1 ;; esac
MAMBA_SSM_DTYPE="${MAMBA_SSM_DTYPE:-}"
case "$MAMBA_SSM_DTYPE" in ""|float32|bfloat16) ;; *) echo "invalid MAMBA_SSM_DTYPE" >&2; exit 1 ;; esac
MAMBA_TRACK_INTERVAL="${MAMBA_TRACK_INTERVAL:-}"
if [ -n "$MAMBA_TRACK_INTERVAL" ] && ! [[ "$MAMBA_TRACK_INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid MAMBA_TRACK_INTERVAL" >&2; exit 1
fi
SPECULATIVE_DRAFT_KV_CACHE_DTYPE="${SPECULATIVE_DRAFT_KV_CACHE_DTYPE:-}"
case "$SPECULATIVE_DRAFT_KV_CACHE_DTYPE" in ""|auto|fp8_e4m3) ;; *) echo "invalid SPECULATIVE_DRAFT_KV_CACHE_DTYPE" >&2; exit 1 ;; esac
# FR-Spec hot-token map (torch .pt of token ids); must be readable under $HOME,
# which the container mounts (--container-mount-home).
SPECULATIVE_TOKEN_MAP="${SPECULATIVE_TOKEN_MAP:-}"
if [ -n "$SPECULATIVE_TOKEN_MAP" ]; then
  case "$SPECULATIVE_TOKEN_MAP" in "$HOME"/*) ;; *) echo "invalid SPECULATIVE_TOKEN_MAP (must be under \$HOME)" >&2; exit 1 ;; esac
  [ -r "$SPECULATIVE_TOKEN_MAP" ] || { echo "SPECULATIVE_TOKEN_MAP is not readable" >&2; exit 1; }
fi
# Explicit decode CUDA-graph batch sizes, comma separated (e.g. 1,2,3,4,5,6,7,8).
DECODE_GRAPH_BS="${DECODE_GRAPH_BS:-}"
if [ -n "$DECODE_GRAPH_BS" ] && ! [[ "$DECODE_GRAPH_BS" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]]; then
  echo "invalid DECODE_GRAPH_BS" >&2; exit 1
fi
WATCHDOG_TIMEOUT_S="${WATCHDOG_TIMEOUT_S:-}"
if [ -n "$WATCHDOG_TIMEOUT_S" ] && ! [[ "$WATCHDOG_TIMEOUT_S" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid WATCHDOG_TIMEOUT_S" >&2; exit 1
fi
export KV_CACHE_DTYPE MAMBA_SSM_DTYPE MAMBA_TRACK_INTERVAL SPECULATIVE_DRAFT_KV_CACHE_DTYPE \
  SPECULATIVE_TOKEN_MAP DECODE_GRAPH_BS WATCHDOG_TIMEOUT_S
if [ -n "$MAMBA_CACHE_SIZE" ] && ! [[ "$MAMBA_CACHE_SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid MAMBA_CACHE_SIZE" >&2; exit 1
fi
# --- prefix-reuse levers (idea/456): longest-prefix-match scheduling and the
# HiCache host-memory KV tier.  Every var below defaults to unset/off, so
# leaving them unset reproduces the exact command line this launcher already
# emits.  Values are validated against the exact flag choices for the pinned
# SGLang revision (see docs/prefix_reuse_serving.md for citations).
SGLANG_SCHEDULE_POLICY="${SGLANG_SCHEDULE_POLICY:-}"
case "$SGLANG_SCHEDULE_POLICY" in
  ""|lpm|random|fcfs|dfs-weight|lof|priority|routing-key|hrrn) ;;
  *) echo "invalid SGLANG_SCHEDULE_POLICY" >&2; exit 1 ;;
esac
# Request-priority scheduling (opt-in): the waiting queue is ordered by each
# request's integer `priority` (higher first), then arrival.  SGLang accepts it
# only with schedule policy fcfs or lof, and gives a request without a
# priority the lowest possible value unless SGLANG_DEFAULT_PRIORITY_VALUE is set.
SGLANG_ENABLE_PRIORITY_SCHEDULING="${SGLANG_ENABLE_PRIORITY_SCHEDULING:-0}"
case "$SGLANG_ENABLE_PRIORITY_SCHEDULING" in 0|1) ;; *) echo "invalid SGLANG_ENABLE_PRIORITY_SCHEDULING" >&2; exit 1 ;; esac
SGLANG_DEFAULT_PRIORITY_VALUE="${SGLANG_DEFAULT_PRIORITY_VALUE:-}"
if [ -n "$SGLANG_DEFAULT_PRIORITY_VALUE" ] && ! [[ "$SGLANG_DEFAULT_PRIORITY_VALUE" =~ ^-?[0-9]+$ ]]; then
  echo "invalid SGLANG_DEFAULT_PRIORITY_VALUE" >&2; exit 1
fi
if [ "$SGLANG_ENABLE_PRIORITY_SCHEDULING" = 1 ]; then
  case "$SGLANG_SCHEDULE_POLICY" in
    fcfs|lof) ;;
    *) echo "SGLANG_ENABLE_PRIORITY_SCHEDULING=1 requires SGLANG_SCHEDULE_POLICY fcfs or lof" >&2; exit 1 ;;
  esac
elif [ -n "$SGLANG_DEFAULT_PRIORITY_VALUE" ]; then
  echo "SGLANG_DEFAULT_PRIORITY_VALUE requires SGLANG_ENABLE_PRIORITY_SCHEDULING=1" >&2; exit 1
fi
SGLANG_ENABLE_HICACHE="${SGLANG_ENABLE_HICACHE:-0}"
case "$SGLANG_ENABLE_HICACHE" in 0|1) ;; *) echo "invalid SGLANG_ENABLE_HICACHE" >&2; exit 1 ;; esac
SGLANG_HICACHE_SIZE_GB="${SGLANG_HICACHE_SIZE_GB:-}"
if [ -n "$SGLANG_HICACHE_SIZE_GB" ] && ! [[ "$SGLANG_HICACHE_SIZE_GB" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid SGLANG_HICACHE_SIZE_GB" >&2; exit 1
fi
SGLANG_HICACHE_WRITE_POLICY="${SGLANG_HICACHE_WRITE_POLICY:-}"
case "$SGLANG_HICACHE_WRITE_POLICY" in
  ""|write_through|write_back|write_through_selective) ;;
  *) echo "invalid SGLANG_HICACHE_WRITE_POLICY" >&2; exit 1 ;;
esac
SGLANG_HICACHE_IO_BACKEND="${SGLANG_HICACHE_IO_BACKEND:-}"
case "$SGLANG_HICACHE_IO_BACKEND" in
  ""|kernel|direct) ;;
  *) echo "invalid SGLANG_HICACHE_IO_BACKEND" >&2; exit 1 ;;
esac
SGLANG_HICACHE_MEM_LAYOUT="${SGLANG_HICACHE_MEM_LAYOUT:-}"
case "$SGLANG_HICACHE_MEM_LAYOUT" in
  ""|layer_first|page_first) ;;
  *) echo "invalid SGLANG_HICACHE_MEM_LAYOUT" >&2; exit 1 ;;
esac
SGLANG_PAGE_SIZE="${SGLANG_PAGE_SIZE:-}"
if [ -n "$SGLANG_PAGE_SIZE" ] && ! [[ "$SGLANG_PAGE_SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid SGLANG_PAGE_SIZE" >&2; exit 1
fi

# Opt in per experiment through the staged metadata, without altering the
# resource-wide default. A caller may set the environment for direct launcher
# use; a present metadata value takes precedence and must be a JSON boolean.
SGLANG_ENABLE_STRICT_THINKING="${SGLANG_ENABLE_STRICT_THINKING:-0}"
case "$SGLANG_ENABLE_STRICT_THINKING" in 0|1) ;; *) echo "invalid SGLANG_ENABLE_STRICT_THINKING" >&2; exit 1 ;; esac
_STRICT_THINKING_STATE_DIR="$(dirname "$THE_LAB_MODEL_READY_FILE")"
_STRICT_THINKING_EXPERIMENT="$_STRICT_THINKING_STATE_DIR/experiment.json"
# The Slurm executor may stage metadata beside the ready file. The preamble
# instead stores readiness under EXP_DIR/eval_state and stages metadata in
# EXP_DIR. Do not search arbitrary ancestors: only this documented layout.
if [ ! -r "$_STRICT_THINKING_EXPERIMENT" ] && [ "$(basename "$_STRICT_THINKING_STATE_DIR")" = "eval_state" ]; then
  _STRICT_THINKING_EXPERIMENT="$(dirname "$_STRICT_THINKING_STATE_DIR")/experiment.json"
fi
if [ -r "$_STRICT_THINKING_EXPERIMENT" ]; then
  _strict_thinking_meta="$(python3 - "$_STRICT_THINKING_EXPERIMENT" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    document = json.load(open(path, encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"cannot read strict-thinking experiment metadata: {exc}")
meta = document.get("meta") if isinstance(document, dict) else None
if meta is None:
    raise SystemExit(0)
if not isinstance(meta, dict):
    raise SystemExit("strict-thinking experiment metadata must contain an object meta")
if "sglang_enable_strict_thinking" not in meta:
    raise SystemExit(0)
value = meta["sglang_enable_strict_thinking"]
if type(value) is not bool:
    raise SystemExit("meta.sglang_enable_strict_thinking must be boolean")
print("1" if value else "0")
PY
)" || { echo "invalid strict-thinking experiment metadata" >&2; exit 1; }
  if [ -n "$_strict_thinking_meta" ]; then
    SGLANG_ENABLE_STRICT_THINKING="$_strict_thinking_meta"
  fi
fi
export SGLANG_SCHEDULE_POLICY SGLANG_ENABLE_HICACHE SGLANG_HICACHE_SIZE_GB \
  SGLANG_HICACHE_WRITE_POLICY SGLANG_HICACHE_IO_BACKEND SGLANG_HICACHE_MEM_LAYOUT SGLANG_PAGE_SIZE \
  SGLANG_ENABLE_STRICT_THINKING SGLANG_ENABLE_PRIORITY_SCHEDULING SGLANG_DEFAULT_PRIORITY_VALUE
export CHUNKED_PREFILL_SIZE DECODE_GRAPH_MAX_BS
for value in "$MAX_RUNNING_REQUESTS" "$CHUNKED_PREFILL_SIZE" "$DECODE_GRAPH_MAX_BS"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || { echo "invalid inference batch/prefill setting" >&2; exit 1; }
done
if [ -n "$GPU_MEMORY_GUARD_SCRIPT" ] && [ ! -r "$GPU_MEMORY_GUARD_SCRIPT" ]; then
  echo "GPU memory guard script is not readable" >&2; exit 1
fi
SGLANG_CONTAINER_IMAGE="${SGLANG_CONTAINER_IMAGE:-lmsysorg/sglang:qwen38flashnext}"
# Keep one named Pyxis import per logical loopback port.  This avoids a socket
# collision for concurrent experiments on one node while allowing later jobs
# assigned the same slot to reuse the node-local converted image.
SGLANG_CONTAINER_NAME="${SGLANG_CONTAINER_NAME:-qwen38flash-p${SGLANG_PORT}}"
export MODEL MODEL_NAME GPU_MEMORY_FRACTION CONTEXT_LENGTH MAX_RUNNING_REQUESTS SGLANG_PORT

if [ -z "${VLLM_API_KEY:-}" ]; then
  : "${MODEL_API_KEY_FILE:=$MODEL_INFERENCE_ROOT/secret.key}"
  VLLM_API_KEY="$(tr -d '\r\n' < "$MODEL_API_KEY_FILE")"
  export VLLM_API_KEY
fi
[ -n "$VLLM_API_KEY" ] || { echo "empty VLLM_API_KEY" >&2; exit 1; }

mkdir -p "$(dirname "$THE_LAB_MODEL_READY_FILE")" "$MODEL_INFERENCE_ROOT/hf_cache"
rm -f "$THE_LAB_MODEL_READY_FILE"
export HF_HOME="$MODEL_INFERENCE_ROOT/hf_cache"
export HF_HUB_CACHE="$HF_HOME/hub"
export TOKENIZERS_PARALLELISM=false

cleanup() {
  if [ -n "${MONITOR_PID:-}" ]; then kill "$MONITOR_PID" 2>/dev/null || true; fi
  if [ -n "${SERVER_STEP_PID:-}" ] && kill -0 "$SERVER_STEP_PID" 2>/dev/null; then
    kill "$SERVER_STEP_PID" 2>/dev/null || true
    wait "$SERVER_STEP_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[the-lab] starting $MODEL_NAME on ${SLURMD_NODENAME:-unknown}:$SGLANG_PORT"
CONTAINER_ENVS=VLLM_API_KEY,MODEL,MODEL_NAME,GPU_MEMORY_FRACTION,CONTEXT_LENGTH,MAX_RUNNING_REQUESTS,SGLANG_PORT,TOKENIZERS_PARALLELISM,CHUNKED_PREFILL_SIZE,DECODE_GRAPH_MAX_BS,MAMBA_CACHE_SIZE,DISABLE_RADIX_CACHE,SPECULATIVE_STEPS,SPECULATIVE_DRAFT_QUANTIZATION,INFERENCE_CAPTURE_SCRIPT,INFERENCE_CAPTURE_DIR,SGLANG_SCHEDULE_POLICY,SGLANG_ENABLE_HICACHE,SGLANG_HICACHE_SIZE_GB,SGLANG_HICACHE_WRITE_POLICY,SGLANG_HICACHE_IO_BACKEND,SGLANG_HICACHE_MEM_LAYOUT,SGLANG_PAGE_SIZE,SGLANG_ENABLE_STRICT_THINKING
CONTAINER_ENVS+=,KV_CACHE_DTYPE,MAMBA_SSM_DTYPE,MAMBA_TRACK_INTERVAL,SPECULATIVE_DRAFT_KV_CACHE_DTYPE,SPECULATIVE_TOKEN_MAP,DECODE_GRAPH_BS,WATCHDOG_TIMEOUT_S
CONTAINER_ENVS+=,SGLANG_ENABLE_PRIORITY_SCHEDULING,SGLANG_DEFAULT_PRIORITY_VALUE
if [ -n "${PYTORCH_CUDA_ALLOC_CONF:-}" ]; then
  export PYTORCH_CUDA_ALLOC_CONF
  CONTAINER_ENVS+=,PYTORCH_CUDA_ALLOC_CONF
fi
CONTAINER_MOUNTS="$MODEL_INFERENCE_ROOT/hf_cache:/hf_cache:rw"
if [ -n "$SPECULATIVE_TOKEN_MAP" ]; then
  # The container does not see $HOME paths (remap-root): mount the validated map
  # file explicitly and hand SGLang the in-container path (768.6 failed with a
  # host path it then tried to download from the Hub).
  CONTAINER_MOUNTS+=,"$SPECULATIVE_TOKEN_MAP:/the_lab_speculative_token_map.pt:ro"
  SPECULATIVE_TOKEN_MAP=/the_lab_speculative_token_map.pt
  export SPECULATIVE_TOKEN_MAP
fi
if [ -n "$INFERENCE_CAPTURE_SCRIPT" ]; then
  cp -- "$INFERENCE_CAPTURE_SCRIPT" "$INFERENCE_CAPTURE_DIR/capture_runtime.py"
  CONTAINER_MOUNTS+=,"$INFERENCE_CAPTURE_DIR:/the_lab_diagnostics:rw"
fi
srun --container-image="$SGLANG_CONTAINER_IMAGE" \
  --container-name="$SGLANG_CONTAINER_NAME" \
  --container-mount-home --container-remap-root \
  --container-mounts="$CONTAINER_MOUNTS" \
  --container-env="$CONTAINER_ENVS" \
  bash -lc 'export HF_HOME=/hf_cache HF_HUB_CACHE=/hf_cache/hub; EXTRA=();
    echo "[the-lab] PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-unset}"
    if [ -n "$INFERENCE_CAPTURE_SCRIPT" ]; then
      python3 /the_lab_diagnostics/capture_runtime.py /the_lab_diagnostics || exit $?
    fi
    if [ -n "$MAMBA_CACHE_SIZE" ]; then EXTRA+=(--max-mamba-cache-size "$MAMBA_CACHE_SIZE"); fi
    if [ "$DISABLE_RADIX_CACHE" = 1 ]; then EXTRA+=(--disable-radix-cache); fi
    if [ "$SPECULATIVE_STEPS" -gt 0 ]; then
      EXTRA+=(--speculative-algorithm NEXTN --speculative-num-steps "$SPECULATIVE_STEPS"
        --speculative-eagle-topk 1 --speculative-num-draft-tokens "$((SPECULATIVE_STEPS + 1))")
      # Rejection sampling needs one vocab for draft and target; an FR-Spec token
      # map shrinks the draft vocab (768.7: 65536 vs 248320), so with a map the
      # thresholds alone are passed, as in the Kaggle milestone-2 server.
      if [ -z "$SPECULATIVE_TOKEN_MAP" ]; then EXTRA+=(--speculative-use-rejection-sampling); fi
      EXTRA+=(--speculative-accept-threshold-single 1.0 --speculative-accept-threshold-acc 1.0)
      if [ -n "$SPECULATIVE_DRAFT_QUANTIZATION" ]; then
        EXTRA+=(--speculative-draft-model-quantization "$SPECULATIVE_DRAFT_QUANTIZATION")
      fi
      if [ -n "$SPECULATIVE_DRAFT_KV_CACHE_DTYPE" ]; then
        EXTRA+=(--speculative-draft-kv-cache-dtype "$SPECULATIVE_DRAFT_KV_CACHE_DTYPE")
      fi
      if [ -n "$SPECULATIVE_TOKEN_MAP" ]; then EXTRA+=(--speculative-token-map "$SPECULATIVE_TOKEN_MAP"); fi
    fi
    if [ -n "$KV_CACHE_DTYPE" ]; then EXTRA+=(--kv-cache-dtype "$KV_CACHE_DTYPE"); fi
    if [ -n "$MAMBA_SSM_DTYPE" ]; then EXTRA+=(--mamba-ssm-dtype "$MAMBA_SSM_DTYPE"); fi
    if [ -n "$MAMBA_TRACK_INTERVAL" ]; then EXTRA+=(--mamba-track-interval "$MAMBA_TRACK_INTERVAL"); fi
    if [ -n "$DECODE_GRAPH_BS" ]; then EXTRA+=(--cuda-graph-bs-decode ${DECODE_GRAPH_BS//,/ }); fi
    if [ -n "$WATCHDOG_TIMEOUT_S" ]; then EXTRA+=(--watchdog-timeout "$WATCHDOG_TIMEOUT_S"); fi
    if [ -n "$SGLANG_SCHEDULE_POLICY" ]; then EXTRA+=(--schedule-policy "$SGLANG_SCHEDULE_POLICY"); fi
    if [ "$SGLANG_ENABLE_PRIORITY_SCHEDULING" = 1 ]; then EXTRA+=(--enable-priority-scheduling); fi
    if [ -n "$SGLANG_DEFAULT_PRIORITY_VALUE" ]; then EXTRA+=(--default-priority-value "$SGLANG_DEFAULT_PRIORITY_VALUE"); fi
    if [ "$SGLANG_ENABLE_HICACHE" = 1 ]; then
      EXTRA+=(--enable-hierarchical-cache)
      if [ -n "$SGLANG_HICACHE_SIZE_GB" ]; then EXTRA+=(--hicache-size "$SGLANG_HICACHE_SIZE_GB"); fi
      if [ -n "$SGLANG_HICACHE_WRITE_POLICY" ]; then EXTRA+=(--hicache-write-policy "$SGLANG_HICACHE_WRITE_POLICY"); fi
      if [ -n "$SGLANG_HICACHE_IO_BACKEND" ]; then EXTRA+=(--hicache-io-backend "$SGLANG_HICACHE_IO_BACKEND"); fi
      if [ -n "$SGLANG_HICACHE_MEM_LAYOUT" ]; then EXTRA+=(--hicache-mem-layout "$SGLANG_HICACHE_MEM_LAYOUT"); fi
    fi
    if [ -n "$SGLANG_PAGE_SIZE" ]; then EXTRA+=(--page-size "$SGLANG_PAGE_SIZE"); fi
    if [ "$SGLANG_ENABLE_STRICT_THINKING" = 1 ]; then EXTRA+=(--enable-strict-thinking); fi
    exec python3 -m sglang.launch_server \
    --model-path "$MODEL" --served-model-name "$MODEL_NAME" --tp 1 \
    --quantization modelopt_fp4 --fp4-gemm-backend flashinfer_cutlass \
    --ple-offload-embedding --mem-fraction-static "$GPU_MEMORY_FRACTION" \
    --context-length "$CONTEXT_LENGTH" --max-running-requests "$MAX_RUNNING_REQUESTS" \
    --chunked-prefill-size "$CHUNKED_PREFILL_SIZE" --reasoning-parser qwen3 --tool-call-parser qwen3_coder \
    --cuda-graph-max-bs-decode "$DECODE_GRAPH_MAX_BS" --host 0.0.0.0 --port "$SGLANG_PORT" \
    --api-key "$VLLM_API_KEY" "${EXTRA[@]}"' &
SERVER_STEP_PID=$!
if [ -n "$GPU_MEMORY_GUARD_SCRIPT" ]; then
  python3 "$GPU_MEMORY_GUARD_SCRIPT" "$$" "$(dirname "$THE_LAB_MODEL_READY_FILE")/gpu_memory.jsonl" &
  MONITOR_PID=$!
fi

deadline=$((SECONDS + SGLANG_READY_TIMEOUT_S))
until curl -fsS --max-time 5 "http://127.0.0.1:$SGLANG_PORT/health" >/dev/null; do
  if [ -n "${MONITOR_PID:-}" ] && ! kill -0 "$MONITOR_PID" 2>/dev/null; then
    echo "GPU memory guard exited unexpectedly" >&2; exit 1
  fi
  if ! kill -0 "$SERVER_STEP_PID" 2>/dev/null; then
    wait "$SERVER_STEP_PID"
    exit 1
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "timed out waiting for SGLang health endpoint" >&2
    exit 1
  fi
  sleep 10
done
# The answer must come from THIS launcher's server. If ours already exited (for
# example its port bind failed), another job's server on the same port answered
# and this run would silently share it (lab thread 2862 msg 5813).
if ! kill -0 "$SERVER_STEP_PID" 2>/dev/null; then
  echo "SGLang health answered on port $SGLANG_PORT, but this launcher's server has exited (port taken by another server?)" >&2
  exit 1
fi

# Health alone can be true before the OpenAI route has completed initialization.
curl -fsS --max-time 180 \
  -H "Authorization: Bearer $VLLM_API_KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL_NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"What is 9 + 8? Answer with only the number.\"}],\"temperature\":0,\"max_tokens\":32,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
  "http://127.0.0.1:$SGLANG_PORT/v1/chat/completions" > "${THE_LAB_MODEL_READY_FILE}.smoke.json"

python3 - "${THE_LAB_MODEL_READY_FILE}.smoke.json" <<'PY'
import json, sys
message = json.load(open(sys.argv[1]))["choices"][0]["message"]
if "17" not in (message.get("content") or message.get("reasoning_content") or ""):
    raise SystemExit(f"unexpected model smoke response: {message!r}")
PY

if ! kill -0 "$SERVER_STEP_PID" 2>/dev/null; then
  echo "SGLang smoke test answered on port $SGLANG_PORT, but this launcher's server has exited (port taken by another server?)" >&2
  exit 1
fi
python3 - "$THE_LAB_MODEL_READY_FILE" <<'PY'
import json, os, sys, time
path = sys.argv[1]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump({"status": "ready", "model": os.environ["MODEL_NAME"],
               "port": int(os.environ["SGLANG_PORT"]), "ready_at": time.time()}, f)
os.replace(tmp, path)
PY
echo "[the-lab] SGLang ready: $MODEL_NAME"
wait "$SERVER_STEP_PID"
