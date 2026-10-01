# 14. Run profiles: the model route from MLX to llama.cpp

Status: v2, 2026-10-02. The GGUF spike is measured, and the Docker skeleton is built and
measured (last section). This document closes R16 as a question
([08-revised-plan.md](08-revised-plan.md)) and records a first number for R19.

## The question

The native profile runs the model on MLX. The Docker profile has no MLX and no GPU, so it runs
the same model through llama.cpp, which reads GGUF files. `mlx_lm.fuse` exports GGUF only for
Llama, Mistral and Mixtral ([01-constraints.md](01-constraints.md) 1.5), so for Qwen the adapter
has to be fused in MLX and converted by llama.cpp's own converter. The spike asks whether that
works, and whether the converted model gives the same answers.

## What was run

`scripts/spikes/gguf_route.sh` runs all of it in 2 minutes 12 seconds on the M3 Pro, after
`brew install llama.cpp`. Outputs go to `var/spikes/gguf/` (3.9 GB, ignored).

| Piece | Value |
|-------|-------|
| Base model | `mlx-community/Qwen2.5-0.5B-Instruct-4bit`: same architecture as the planned 7B, minutes instead of an hour |
| MLX | `mlx-lm[train]==0.31.3` in a throwaway environment (`uv run --with`); `uv.lock` unchanged |
| llama.cpp | 0.5.0, build 11146, commit `7fe450e`, from Homebrew; the converter from the same tag `b11146` |
| Toy task | Map a line-item label to its canonical id, from the English aliases in `fra_core`'s taxonomy |
| Training | 40 examples over 23 ids, 100 iterations, validation loss 6.39 to 0.20, peak memory 1.0 GB |
| Held out | 10 labels the adapter never saw, for ids it did see |

The steps, in order:

1. `mlx_lm lora` trains the adapter on the 4-bit base (11.8 MB).
2. `mlx_lm fuse --dequantize` writes a full-precision model directory (988 MB, bfloat16).
3. `convert_hf_to_gguf.py --outtype f16` writes the f16 GGUF.
4. `llama-quantize` writes Q4_K_M, Q5_K_M, Q6_K and Q8_0 from the f16 file.
5. `llama-server -ngl 0 --jinja` serves each file on the CPU. The ten prompts go through
   `/v1/chat/completions` with the same system and user messages at temperature 0, using the
   chat template stored in the GGUF.

## Result

The route works. The f16 GGUF gives the same answer as MLX on 10 of 10 prompts.

| Model | File | Same answer as MLX with the adapter |
|-------|-----:|:-----------------------------------:|
| MLX base, no adapter (control) | | 0 / 10 |
| GGUF f16 | 960 MB | 10 / 10 |
| GGUF Q8_0 | 521 MB | 9 / 10 |
| GGUF Q6_K | 494 MB | 10 / 10 |
| GGUF Q5_K_M | 410 MB | 9 / 10 |
| GGUF Q4_K_M | 390 MB | 8 / 10 |

The control matters: the base model alone answers with strings of digits, so the agreement
comes from the adapter having survived the fuse and the conversion, not from the base model
already knowing the task.

"Same answer" is the measure, not "right answer". The toy adapter is weak: it names the right
canonical id on 2 of the 10 unseen labels, and invents ids such as `cost_of_use` on others.
That is what 100 iterations on a 0.5B model buy, and it makes the comparison stricter, since
invented answers are easier to disturb than memorized ones.

### Why Q4_K_M misses, and why it is not a finding against the route

Only two prompts ever change, the same two at every level, and Q8_0 changes one that Q6_K does
not. Loss that came from coarser quantization would grow as the files shrink. This does not.

The two prompts are near ties in the f16 model itself. At the step where its first and second
choice are closest:

| Label | First choice | Second choice |
|-------|--------------|---------------|
| revenue from contracts with customers | `oper` 0.209 | `ppe` 0.206 |
| profit for the period | `_tax` 0.418 | end of answer 0.412 |
| the other eight, narrowest | 0.156 | 0.110 |

At temperature 0 the model takes the first choice, so when two choices are 0.3 to 0.6 points
apart any rounding of the weights can swap them. The eight prompts with a clear first choice
hold at every level.

So the plan's bar of 9 in 10 is met by f16, Q8_0, Q6_K and Q5_K_M, and missed by Q4_K_M on two
coin flips. An exact-match count over ten prompts cannot tell a tie from a real change; week 3
needs a better measure (below).

## Speed

`llama-bench` on the Q4_K_M file, 6 threads, 128 generated tokens, three repeats. The serving
run cannot measure this, because the toy model answers in three tokens.

| Backend | Prompt, tokens/s | Generation, tokens/s |
|---------|-----------------:|---------------------:|
| CPU only (`-ngl 0`) | 367 | 79 |
| Metal | 2,795 | 110 |

This is the 0.5B model on the Mac's own CPU, not in a container. The 7B model has about 15
times the parameters, and CPU generation scales roughly with size, so the Docker profile lands
near the 5 tokens/s line of R19 before the VM takes its share. That is an estimate, not a
measurement: R19 stays open until the 7B file is benchmarked inside the container.

## What failed, and the fix

`mlx_lm fuse` stopped with `IncompleteSnapshotError`. It reads the base model's whole cached
snapshot with `local_files_only=True`, but `mlx_lm` downloads only the model files, so
`README.md` and `.gitattributes` are missing and `huggingface_hub` refuses the snapshot. One
call to `snapshot_download` for the base model before fusing completes it. The script does this.

Nothing else failed. Qwen2's tokenizer and chat template convert without flags.

## Not run

Route B (the unfused base as GGUF, the adapter converted with `convert_lora_to_gguf.py` and
loaded with `--lora`) was not tried, because route A agrees. It would first need the adapter
rewritten: MLX writes `adapters.safetensors` with its own config, and llama.cpp's converter
reads the PEFT layout. It is the fallback if a fused 7B model ever fails to convert.

Nothing in the spike was run in Docker. The CPU-only figures come from `-ngl 0` on macOS.

## Decision for week 3

- **Route:** train the QLoRA adapter on the 4-bit MLX base as planned, then `fuse --dequantize`,
  `convert_hf_to_gguf.py` to f16, `llama-quantize`. No change to the training plan.
- **Pin** llama.cpp's binaries and its converter to the same build tag.
- **Complete the snapshot** before fusing.
- **Quantization level is chosen by measurement, not assumed.** After the real adapter is
  trained, compare MLX with Q4_K_M and Q5_K_M on at least 50 held-out labels, and record the
  first-to-second margin for every answer that differs. A difference on a near tie is noise; a
  confident answer that changes is a reason to move up a level. `gguf_spike.py margins` shows
  how to read the margin from `llama-server`.
- **Disk:** the dequantized 7B directory and its f16 GGUF are each about 15 GB (16-bit weights
  for 7.6 billion parameters), next to the quantized file. They are intermediate and can be
  deleted once the quantized file is checked.

## Open

- R19: generation speed of the 7B GGUF inside the container.
- Whether agreement holds on Arabic labels. The toy task was English only.

## The Docker skeleton

`make docker-up` (`docker compose up -d --build --wait`) starts two services from one image and
fails if a service exits at start or the api does not turn healthy, and `make dev` serves the same
API natively. Both answer `/health` with their own profile name. The profile comes from
`FRA_PROFILE`, a closed set (`native`, `docker`) read once by `fra_core.profile`; unset or
misspelled, the process stops and says so. `make dev` sets `native`, the image sets `docker`.

| Service | Today | Later |
|---------|-------|-------|
| `api` | FastAPI 0.141.1 under uvicorn: `/health` (status, package version, profile) and a one-line HTML page at `/`, and no other route. The generated `/docs`, `/redoc` and `/openapi.json` are switched off and return with the real API in week 4. Healthy once `/health` answers with profile `docker` | Upload, job status, results (week 4) |
| `worker` | The same image. Imports `fra_ingest`, prints the profile and sleeps. Proves the image carries docling and the locked dependencies | The job runner from `apps/worker` (week 4) |
| `llm` | Defined, not started. `ghcr.io/ggml-org/llama.cpp:server` on the CPU, reading `var/models/model.gguf` from a read-only mount. Behind the `llm` compose profile, and `docker compose --profile llm config` validates | Serves the fused adapter as GGUF by the route in "Decision for week 3"; needs the file that week 3 produces |

Ports live in `.env` (`FRA_API_PORT`, `FRA_LLM_PORT`), which compose and the Makefile both read.
The Makefile accepts only blank lines, `#` comments and `NAME=value` lines there (values of
letters, digits and `. _ : / -`, each name once), and stops with the file and line number on
anything else, or when the file or a port is missing. Compose
passes a port to a container only through `environment` and the port mapping, both from the same
variable, so `FRA_API_PORT=9000 docker compose up -d` (or `make docker-up`) maps, probes and
listens on 9000. The health path and the rule for "healthy" live in `fra_api.healthcheck`, which
compose runs inside the container and `make docker-health` runs from the host.

### Image

The image is a frozen sync of `uv.lock`, so it holds the locked, hash-checked versions.

| Piece | Value |
|-------|-------|
| Base | `python:3.12-slim`, multi-stage, non-root user |
| mac extra | Not installed. The lock gives the `ocrmac` dependency the marker `sys_platform == 'darwin'`, so a Linux sync skips it and the pyobjc packages that only it pulls in; `import ocrmac` fails in the container |
| torch | 2.14.0+cpu and torchvision 0.29.0+cpu on Linux, 2.14.0 and 0.29.0 on the Mac: the same versions on both profiles. The root `pyproject.toml` pins both and sends them to PyTorch's CPU index on Linux |
| CUDA | Before this setting the lock held 19 packages the container cannot use (15 `nvidia-*`, 3 `cuda-*`, `triton`). It now holds none, and the image has none (checked: no installed distribution starts with `nvidia`, `cuda` or `triton`); `torch.cuda.is_available()` is `False` |
| Size | 1.68 GB, 1,679,494,773 bytes (`docker image inspect`); most of it is the virtual environment, 1,620,584 KiB by `du -sk` |
| Cold build | 148 s with `--no-cache` on the M3 Pro (Docker 27.3.1, aarch64, 12 CPUs, 14.1 GB for the VM per `docker info`), base images already pulled. The dependency layer is 90 s of it (wheel download 49 s, install 17 s, bytecode compile 18 s), and copying the environment into the final stage 29 s. A source-only change reuses the dependency layer |
| Start | `docker compose up -d --wait` returned in 6.6 to 6.9 s over three runs, with the healthcheck interval at 5 s |
| Stop | `docker compose down` with both services up takes 0.6 to 0.7 s; `docker compose stop worker` takes 0.3 s, because `init: true` forwards the stop signal to the sleeping process |

The build context is an allowlist (`.dockerignore`): the workspace sources, the lockfile and
`configs/`.

### Deliberately missing

- No job queue, database or upload page (week 4).
- No `apps/worker` package; the worker service is a command in `compose.yaml`.
- No model in the image. The `llm` service waits for a GGUF file and was never started.
- No OCR engine in the image. The Apple Vision engine is Mac-only; the Linux engines arrive with
  week 2, and the image must grow them then.
- Not measured: the image on x86-64. The numbers above are aarch64; the lock does carry hashed
  CPU wheels for x86-64, but nothing was built there.

### Left for later

- **Writable state.** `/app` is owned by root and `configs/ingest.toml` puts artifacts under
  `var/artifacts`, so the `app` user cannot write there until a volume owned by uid 1000 is
  mounted at `/app/var`, and another for the docling model cache.
- **Memory limits** for the services.
- **Pinned images.** `llama.cpp:server` and `python:3.12-slim` are floating tags; pin them by
  digest when the model service first runs.
- **Healthcheck cadence.** Use `start_interval` with a longer `interval` once the API does real
  work.
- **Build cache.** A uv cache mount for the dependency layer.
- **Restart policy,** with the job queue.
- **x86-64** is not built.
- **OpenCV.** `import cv2` fails in the image: `ImportError: libxcb.so.1: cannot open shared
  object file`. The lock has `opencv-python`, not the headless build. The slim base lacks
  `libxcb.so.1`, and also `libGL.so.1`, `libglib-2.0.so.0`, `libSM.so.6` and `libXext.so.6`
  (checked by file lookup, not by importing). The week 2 OCR work needs either the system
  libraries or the headless wheel in the lock. `import docling` and `fra_ingest` work today,
  and the taxonomy loads (47 items).
