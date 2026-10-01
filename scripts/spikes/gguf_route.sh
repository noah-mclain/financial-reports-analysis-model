#!/usr/bin/env bash
# The GGUF route for a fused MLX adapter, end to end (docs/blueprint/14-run-profiles.md).
#
#   brew install llama.cpp
#   scripts/spikes/gguf_route.sh
#
# Trains a toy LoRA with MLX on a 4-bit Qwen2.5 model, fuses and dequantizes it, converts it to
# GGUF with llama.cpp's converter, quantizes it, serves each file with llama-server on the CPU
# and compares its answers with the MLX model's. Everything lands in var/spikes/gguf (ignored).
# MLX runs in a throwaway environment, so uv.lock does not change.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

WORK=var/spikes/gguf
BASE=mlx-community/Qwen2.5-0.5B-Instruct-4bit
PORT=8090
QUANTS=(Q4_K_M Q5_K_M Q6_K Q8_0)
SPIKE=(uv run python scripts/spikes/gguf_spike.py)
MLX=(uv run --with "mlx-lm[train]==0.31.3" python)

for tool in llama-server llama-quantize llama-bench; do
    command -v "$tool" >/dev/null || { echo "$tool not found: brew install llama.cpp" >&2; exit 1; }
done
mkdir -p "$WORK/logs" "$WORK/answers"

# 1. The converter has to match the installed binaries: the build number is the release tag.
build=$(llama-server --version 2>&1 | sed -n 's/.*build \([0-9]*\).*/\1/p')
if [ ! -d "$WORK/llama.cpp" ]; then
    git clone --quiet --depth 1 --branch "b$build" https://github.com/ggml-org/llama.cpp \
        "$WORK/llama.cpp"
fi

# 2. A toy adapter: 40 label-to-id examples, 100 iterations.
"${SPIKE[@]}" data
"${MLX[@]}" -m mlx_lm lora --model "$BASE" --train --data "$WORK/data" --iters 100 \
    --batch-size 4 --learning-rate 2e-4 --steps-per-report 20 --steps-per-eval 50 \
    --val-batches 1 --adapter-path "$WORK/adapter" --seed 0 >"$WORK/logs/train.log" 2>&1
grep "Val loss" "$WORK/logs/train.log"

# 3. Reference answers from MLX, and the base model alone as a control.
"${MLX[@]}" scripts/spikes/gguf_spike.py mlx --adapter "$WORK/adapter" \
    --out "$WORK/answers/mlx_adapter.json" 2>/dev/null
"${MLX[@]}" scripts/spikes/gguf_spike.py mlx --out "$WORK/answers/mlx_base.json" 2>/dev/null

# 4. Fuse and dequantize, convert, quantize.
# mlx_lm.fuse reads the whole cached snapshot offline, but mlx_lm only downloads the model
# files, and huggingface_hub refuses the incomplete snapshot. Completing it first avoids that.
"${MLX[@]}" -c "from huggingface_hub import snapshot_download; snapshot_download('$BASE')" \
    >/dev/null 2>&1
rm -rf "$WORK/fused"
"${MLX[@]}" -m mlx_lm fuse --model "$BASE" --adapter-path "$WORK/adapter" \
    --save-path "$WORK/fused" --dequantize >"$WORK/logs/fuse.log" 2>&1
uv run --with "$WORK/llama.cpp/gguf-py" --with sentencepiece --with "protobuf<5" \
    python "$WORK/llama.cpp/convert_hf_to_gguf.py" "$WORK/fused" \
    --outfile "$WORK/fused-f16.gguf" --outtype f16 >"$WORK/logs/convert.log" 2>&1
for quant in "${QUANTS[@]}"; do
    llama-quantize "$WORK/fused-f16.gguf" "$WORK/fused-$quant.gguf" "$quant" \
        >"$WORK/logs/quantize-$quant.log" 2>&1
done

# 5. Serve each file on the CPU, as Docker will, and compare with the MLX answers.
serve() {
    llama-server -m "$WORK/fused-$1.gguf" --host 127.0.0.1 --port "$PORT" -ngl 0 -c 2048 \
        --jinja >"$WORK/logs/server-$1.log" 2>&1 &
    server_pid=$!
    for _ in $(seq 60); do
        curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && return 0
        sleep 1
    done
    echo "llama-server did not come up for $1" >&2
    return 1
}
for name in f16 "${QUANTS[@]}"; do
    serve "$name"
    echo "== $name, $(du -h "$WORK/fused-$name.gguf" | cut -f1)"
    "${SPIKE[@]}" server --url "http://127.0.0.1:$PORT" --out "$WORK/answers/gguf_$name.json"
    [ "$name" = f16 ] && "${SPIKE[@]}" margins --url "http://127.0.0.1:$PORT"
    kill "$server_pid" && wait "$server_pid" 2>/dev/null || true
    # compare exits 1 below 9 of 10; the table is the result, so the script carries on.
    "${SPIKE[@]}" compare "$WORK/answers/mlx_adapter.json" "$WORK/answers/gguf_$name.json" \
        | grep -E "DIFF|agreement" || true
done

# Generation speed, CPU only and with Metal. The toy model answers in three tokens, so the
# serving run above says nothing about speed.
llama-bench -m "$WORK/fused-Q4_K_M.gguf" -ngl 0,99 -n 128 -p 512 -r 3 2>/dev/null | grep "^|"
