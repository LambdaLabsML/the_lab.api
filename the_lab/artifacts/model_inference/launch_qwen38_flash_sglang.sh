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
  if [ -n "${SERVER_STEP_PID:-}" ] && kill -0 "$SERVER_STEP_PID" 2>/dev/null; then
    kill "$SERVER_STEP_PID" 2>/dev/null || true
    wait "$SERVER_STEP_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[the-lab] starting $MODEL_NAME on ${SLURMD_NODENAME:-unknown}:$SGLANG_PORT"
srun --container-image="$SGLANG_CONTAINER_IMAGE" \
  --container-name="$SGLANG_CONTAINER_NAME" \
  --container-mount-home --container-remap-root \
  --container-mounts="$MODEL_INFERENCE_ROOT/hf_cache:/hf_cache:rw" \
  --container-env=VLLM_API_KEY,MODEL,MODEL_NAME,GPU_MEMORY_FRACTION,CONTEXT_LENGTH,MAX_RUNNING_REQUESTS,SGLANG_PORT,TOKENIZERS_PARALLELISM \
  bash -lc 'export HF_HOME=/hf_cache HF_HUB_CACHE=/hf_cache/hub; exec python3 -m sglang.launch_server \
    --model-path "$MODEL" --served-model-name "$MODEL_NAME" --tp 1 \
    --quantization modelopt_fp4 --fp4-gemm-backend flashinfer_cutlass \
    --ple-offload-embedding --mem-fraction-static "$GPU_MEMORY_FRACTION" \
    --context-length "$CONTEXT_LENGTH" --max-running-requests "$MAX_RUNNING_REQUESTS" \
    --chunked-prefill-size 2048 --reasoning-parser qwen3 --tool-call-parser qwen3_coder \
    --cuda-graph-max-bs-decode 1 --host 0.0.0.0 --port "$SGLANG_PORT" \
    --api-key "$VLLM_API_KEY"' &
SERVER_STEP_PID=$!

deadline=$((SECONDS + SGLANG_READY_TIMEOUT_S))
until curl -fsS --max-time 5 "http://127.0.0.1:$SGLANG_PORT/health" >/dev/null; do
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
