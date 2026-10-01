#!/usr/bin/env bash
# The GGUF route for a fused MLX adapter, end to end (docs/blueprint/14-run-profiles.md).
#
#   brew install llama.cpp
#   scripts/spikes/gguf_route.sh
#
# Trains a toy LoRA with MLX on a 4-bit Qwen2.5 model, fuses and dequantizes it, converts it to
# GGUF with llama.cpp's converter, quantizes it, serves each file with llama-server on the CPU
# and compares its answers with the MLX model's. Everything lands in var/spikes/gguf (ignored),
# and every step's output in var/spikes/gguf/logs. MLX runs in a throwaway environment, so the
# spike leaves uv.lock unchanged.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# The work directory, the base model and the server address are set here and nowhere else:
# gguf_spike.py takes them as arguments.
WORK=var/spikes/gguf
BASE=mlx-community/Qwen2.5-0.5B-Instruct-4bit
PORT=8090
URL="http://127.0.0.1:$PORT"
QUANTS=(Q4_K_M Q5_K_M Q6_K Q8_0)
SPIKE=(uv run python scripts/spikes/gguf_spike.py)
MLX=(uv run --with "mlx-lm[train]==0.31.3" python)
# Model caches stay inside the repository (docs/blueprint/01-constraints.md 1.4).
export HF_HOME="$PWD/var/hf"

for tool in llama-server llama-quantize llama-bench; do
    command -v "$tool" >/dev/null || { echo "$tool not found: brew install llama.cpp" >&2; exit 1; }
done
mkdir -p "$WORK/logs" "$WORK/answers"

# A server left running would answer for the next model. Whatever stops the script stops it.
server_pid=""
stop_server() {
    [ -n "$server_pid" ] || return 0
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    server_pid=""
}
trap stop_server EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 1. The converter has to match the installed binaries: the build number is the release tag.
build=$(llama-server --version 2>&1 | sed -n 's/.*build \([0-9][0-9]*\).*/\1/p')
if [ -z "$build" ]; then
    echo "no build number in the output of llama-server --version" >&2
    exit 1
fi
if [ ! -d "$WORK/llama.cpp" ]; then
    git clone --quiet --depth 1 --branch "b$build" https://github.com/ggml-org/llama.cpp \
        "$WORK/llama.cpp"
fi
if ! git -C "$WORK/llama.cpp" tag --points-at HEAD | grep -qx "b$build"; then
    echo "$WORK/llama.cpp is not at tag b$build, the installed build: remove it and rerun" >&2
    exit 1
fi

# 2. A toy adapter: 40 label-to-id examples, 100 iterations.
"${SPIKE[@]}" data --work "$WORK"
"${MLX[@]}" -m mlx_lm lora --model "$BASE" --train --data "$WORK/data" --iters 100 \
    --batch-size 4 --learning-rate 2e-4 --steps-per-report 20 --steps-per-eval 50 \
    --val-batches 1 --adapter-path "$WORK/adapter" --seed 0 >"$WORK/logs/train.log" 2>&1
grep "Val loss" "$WORK/logs/train.log"

# 3. Reference answers from MLX, and the base model alone as a control.
"${MLX[@]}" scripts/spikes/gguf_spike.py mlx --work "$WORK" --model "$BASE" \
    --adapter "$WORK/adapter" --out "$WORK/answers/mlx_adapter.json" 2>"$WORK/logs/mlx-adapter.log"
"${MLX[@]}" scripts/spikes/gguf_spike.py mlx --work "$WORK" --model "$BASE" \
    --out "$WORK/answers/mlx_base.json" 2>"$WORK/logs/mlx-base.log"

# 4. Fuse and dequantize, convert, quantize.
# mlx_lm.fuse reads the whole cached snapshot offline, but mlx_lm only downloads the model
# files, and huggingface_hub refuses the incomplete snapshot. Completing it first avoids that.
"${MLX[@]}" -c "from huggingface_hub import snapshot_download; print(snapshot_download('$BASE'))" \
    >"$WORK/logs/snapshot.log" 2>&1
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
    local file="$WORK/fused-$1.gguf"
    if curl -sf "$URL/health" >/dev/null; then
        echo "port $PORT already answers /health: stop that server, it is not this run's" >&2
        return 1
    fi
    llama-server -m "$file" --host 127.0.0.1 --port "$PORT" -ngl 0 -c 2048 \
        --jinja >"$WORK/logs/server-$1.log" 2>&1 &
    server_pid=$!
    for _ in $(seq 60); do
        if ! kill -0 "$server_pid" 2>/dev/null; then
            echo "llama-server exited for $1: see $WORK/logs/server-$1.log" >&2
            return 1
        fi
        if curl -sf "$URL/health" >/dev/null; then
            # The answer has to come from the server started here, with this file loaded.
            if kill -0 "$server_pid" 2>/dev/null && curl -sf "$URL/props" | grep -qF "$file"; then
                return 0
            fi
            echo "port $PORT is answered by a server that did not load $file" >&2
            return 1
        fi
        sleep 1
    done
    echo "llama-server did not come up for $1" >&2
    return 1
}
for name in f16 "${QUANTS[@]}"; do
    serve "$name"
    echo "== $name, $(wc -c <"$WORK/fused-$name.gguf" | tr -d ' ') bytes"
    "${SPIKE[@]}" server --work "$WORK" --url "$URL" --out "$WORK/answers/gguf_$name.json"
    if [ "$name" = f16 ]; then
        "${SPIKE[@]}" margins --work "$WORK" --url "$URL" | tee "$WORK/logs/margins-f16.log"
    fi
    stop_server
    # compare exits 1 below the bar; the table is the result, so the script carries on. Any
    # other failure stops it, and so does an output without the agreement line.
    status=0
    "${SPIKE[@]}" compare "$WORK/answers/mlx_adapter.json" "$WORK/answers/gguf_$name.json" \
        >"$WORK/logs/compare-$name.log" || status=$?
    if [ "$status" -gt 1 ]; then
        echo "compare failed for $name with exit $status" >&2
        exit "$status"
    fi
    grep -E "DIFF|agreement" "$WORK/logs/compare-$name.log"
    grep -q "^agreement " "$WORK/logs/compare-$name.log"
done

# Generation speed, CPU only and with Metal. The toy model answers in three tokens, so the
# serving run above says nothing about speed.
llama-bench -m "$WORK/fused-Q4_K_M.gguf" -ngl 0,99 -n 128 -p 512 -r 3 \
    >"$WORK/logs/bench.log" 2>&1
grep "^|" "$WORK/logs/bench.log"
