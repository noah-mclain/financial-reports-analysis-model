# 14. Run profiles: the model route from MLX to llama.cpp, and the Docker skeleton

Status: v5, 2026-10-05. The GGUF spike is measured, on two runs, and the Docker skeleton is
built and measured (last section); a hosted Linux container smoke run now covers x86-64. This
document closes R16 as a question
([08-revised-plan.md](08-revised-plan.md)): the route works on a small model. The route uses a
step that [01-constraints.md](01-constraints.md) 1.4 forbade on this machine; the owner amended
that rule on 2026-10-02 for this export only, under conditions (see "Decision for week 3"). The
7B route has not been run: the rehearsal comes first. This document records a first number for
R19.

## The question

The native profile runs the model on MLX. The Docker profile has no MLX and no GPU, so it runs
the same model through llama.cpp, which reads GGUF files. `mlx_lm.fuse` exports GGUF only for
Llama, Mistral and Mixtral ([01-constraints.md](01-constraints.md) 1.5), so for Qwen the adapter
has to be fused in MLX and converted by llama.cpp's own converter. The spike asks whether that
works, and whether the converted model gives the same answers.

## What was run

`scripts/spikes/gguf_route.sh` runs all of it, after `brew install llama.cpp`. It was run twice:
once on 1 October, and again on 2 October after the script was changed to switch off
`llama-server`'s prompt cache and to save the margins and the benchmark. The first run took 2
minutes 12 seconds on the M3 Pro. So did the second, with the base model already in `var/hf`.
The script also ran three more times, in part or in full, to prove its failure handling (port
already taken, a failure midway, a TERM signal); only the last full run's outputs are kept.
Where the two runs differ, both results are given. Outputs go to `var/spikes/gguf/`
(4.08 GB, 4,084,671,488 bytes, ignored): the second run's logs and answers, and the first run's
under `run1/`.

| Piece | Value |
|-------|-------|
| Base model | `mlx-community/Qwen2.5-0.5B-Instruct-4bit`, revision `a5339a4131f135d0fdc6a5c8b5bbed2753bbe0f3`: same architecture as the planned 7B, minutes instead of an hour |
| MLX | `mlx-lm[train]==0.31.3` in a throwaway environment (`uv run --with`); the spike leaves `uv.lock` unchanged. Only `mlx-lm` is pinned: the environment resolved `mlx` 0.32.3, not the 0.32.2 that [01-constraints.md](01-constraints.md) 1.5 pins |
| Model cache | `HF_HOME=var/hf`, as 1.4 requires, set by the script (290 MB). The first run did not set it, and the model went to `~/.cache/huggingface` |
| llama.cpp | 0.5.0, build 11146, commit `7fe450e`, from Homebrew; the converter from the same tag `b11146` |
| Toy task | Map a line-item label to its canonical id, from the English aliases in `fra_core`'s taxonomy |
| Training | 40 examples over 23 ids, 100 iterations, seed 0, peak memory 1.0 GB. Loss on five training rows, prompt tokens included (not held out), went from 6.39 to 0.20 |
| Held out | 10 labels the adapter never saw, for ids it did see |

The steps, in order:

1. `mlx_lm lora` trains the adapter on the 4-bit base (11.8 MB).
2. `mlx_lm fuse --dequantize` writes a float16 model directory (1.00 GB, of which
   `model.safetensors` is 988 MB). The flag is required, not optional: without it
   `LoRALinear.fuse` (`mlx_lm/tuner/lora.py`, 0.31.3) puts each fused layer back into 4 bits with
   `nn.QuantizedLinear.from_linear`.
3. `convert_hf_to_gguf.py --outtype f16` writes the f16 GGUF.
4. `llama-quantize` writes Q4_K_M, Q5_K_M, Q6_K and Q8_0 from the f16 file.
5. `llama-server -ngl 0 --jinja` serves each file on the CPU. The ten prompts go through
   `/v1/chat/completions` with the same system and user messages at temperature 0, using the
   chat template stored in the GGUF. In the second run every request also sends
   `"cache_prompt": false`.

## Result

The route works. The f16 GGUF gives the same answer as MLX on 10 of 10 prompts, in both runs.

What is compared: the MLX reference is the 4-bit base with the adapter loaded unfused. The GGUF
is the dequantized base plus the adapter's delta, stored as 16-bit, then quantized again by
`llama-quantize`.

| Model | File | Bits per weight | Same answer as MLX, second run | First run |
|-------|-----:|----------------:|:------------------------------:|:---------:|
| MLX base, no adapter (control) | | | 0 / 10 | 0 / 10 |
| GGUF f16 | 994 MB | 16.00 | 10 / 10 | 10 / 10 |
| GGUF Q8_0 | 531 MB | 8.50 | 9 / 10 | 9 / 10 |
| GGUF Q6_K | 506 MB | 8.09 | 10 / 10 | 10 / 10 |
| GGUF Q5_K_M | 420 MB | 6.71 | 9 / 10 | 9 / 10 |
| GGUF Q4_K_M | 398 MB | 6.35 | 7 / 10 | 8 / 10 |

File sizes are byte counts in decimal MB, the same in both runs. Bits per weight are from
`logs/quantize-*.log`.

The control matters: the base model alone answers with strings of digits, so the agreement
comes from the adapter having survived the fuse and the conversion, not from the base model
already knowing the task.

"Same answer" is the measure, not "right answer". The toy adapter is weak: it names the right
canonical id on 2 of the 10 unseen labels, and invents ids such as `cost_of_use` on others.
That is what 100 iterations on a 0.5B model buy, and it makes the comparison stricter, since
invented answers are easier to disturb than memorized ones.

### These files are not the levels their names say

`llama-quantize` reports that 144 of 290 tensors needed fallback quantization for Q4_K_M, Q5_K_M
and Q6_K. The K formats need a row length divisible by 256, and the 0.5B model's hidden size is
896. The other 121 tensors are f32 and stay as they are. In the "Q4_K_M" file, the 169 quantized
tensors are stored as 12 q4_K, 12 q6_K, 132 q5_0 and 13 q8_0: the 144 fallbacks are the 132 q5_0
and 12 of the q8_0, and the 13th q8_0 is the token embedding. The result is 6.35 bits per weight,
where a true Q4_K_M is about 4.9 (llama.cpp's figure, not measured here).

So these files are finer than the same names will be on the 7B, and the table says nothing
about a true Q4_K_M, Q5_K_M or Q6_K. The plan's premise, that the route depends on the
architecture and not the size, holds for the fuse and the conversion. It does not hold for the
quantize step.

### Which answers change

| Level | Prompts that differ from MLX |
|-------|------------------------------|
| Q8_0 | "revenue from contracts with customers" only |
| Q6_K | none |
| Q5_K_M | "profit for the period" only |
| Q4_K_M | both of those; in the second run also "investment income" |

Every other answer is the same in the two runs, and so are the MLX answers.

The table neither shows nor rules out a loss that grows as the bits fall. The order is not
monotone: Q8_0 at 8.50 bits changes an answer that Q6_K at 8.09 does not. But the file with the
fewest bits changes the most. Ten prompts cannot separate the two readings.

### The prompt cache changes an answer on its own

The first run left `llama-server`'s prompt cache on. Its logs show 37 prompt tokens evaluated
for the first request and 7 to 12 for the rest, because the shared prefix was reused. The second
run switched it off, and every request evaluated 33 to 38 tokens.

Switching the cache reproduces the difference on the same file. The first run's GGUF files were
not hashed, so that the two runs' files were otherwise identical is not shown. Serving the
second run's files and asking the three prompts that ever change, with the cache on and then off
(`logs/cache-probe.log`, two passes per setting): for the Q4_K_M file, with the cache on,
"investment income" gets `investment_income`, as in the first run; with it off, `nip_income`. Each
setting repeats itself exactly on a second pass. For the other four files the answers to those
three prompts are the same with the cache on and off. The other seven prompts were not asked. So
the same file, at temperature 0, gives two answers to one prompt
depending on whether a prefix was cached.

### Why the answers change: consistent with near ties, not shown

The margins below are from the f16 GGUF in the second run (`logs/margins-f16.log`): for each
prompt, the step where the first and second choice are closest. Log-odds is the natural log of
the ratio of the two probabilities: 0 is a tie, 0.69 is twice as likely.

| Label | First choice | Second choice | Log-odds | Changes at |
|-------|--------------|---------------|---------:|------------|
| profit for the period | `_tax` 0.417 | end of answer 0.413 | 0.008 | Q5_K_M, Q4_K_M |
| revenue from contracts with customers | `oper` 0.209 | `ppe` 0.205 | 0.018 | Q8_0, Q4_K_M |
| investment income | `investment` 0.380 | `nip` 0.285 | 0.289 | Q4_K_M, cache off |
| borrowing costs | `ppe` 0.156 | `oper` 0.111 | 0.343 | |
| depreciation | `isation` 0.602 | `ization` 0.393 | 0.428 | |
| profit before zakat and income tax | `profit` 0.398 | `net` 0.174 | 0.828 | |
| tax expense | end of answer 0.743 | `_exp` 0.244 | 1.113 | |
| cost of goods sold | `_use` 0.600 | `_re` 0.162 | 1.308 | |
| administrative expenses | `_costs` 0.683 | `_cost` 0.179 | 1.341 | |
| operating income | `ating` 0.965 | `uted` 0.016 | 4.108 | |

What supports the near-tie reading: the three prompts that ever change are the three with the
narrowest margins, and the two that change at more than one level are within 0.02 of a tie.

All three changed answers also begin with the f16 model's second choice at the step the table
reports: `ppe_net` for "revenue from contracts with customers", `nip_income` for "investment
income", and, for "profit for the period", an answer that ends after `profit_before`. That is
the strongest support on disk for the near-tie reading.

What it does not show:

- The margins are from the f16 GGUF only. The bar is defined against the MLX reference, and its
  margins were not read.
- The third prompt is not a tie. At 0.289 its first choice is 1.3 times as likely as its
  second, and it still changed. The next two, at 0.343 and 0.428, did not. Nine of the ten
  margins are under 1.4, so on this adapter almost every answer is exposed, and there is no
  clear line between a near tie and a safe answer.
- The first run's margins were printed to the terminal and not saved. The three written down
  then (0.209 against 0.206, 0.418 against 0.412, 0.156 against 0.110) are within 0.001 of the
  second run's.

What would show the reading is wrong: an answer that changes on a wide margin; a wide margin in
MLX on a prompt that changes in GGUF; or, on a larger sample, a rate of change that rises as
the level gets coarser.

So the plan's bar of 9 in 10 is met by f16, Q8_0, Q6_K and Q5_K_M, and missed by the file named
Q4_K_M, at 7 of 10 with the prompt cache off and 8 of 10 with it on. An exact-match count over
ten prompts cannot tell a tie from a real change; week 3 needs a better measure (below).

## Speed

`llama-bench` on the "Q4_K_M" file, which is the 6.35 bits per weight file above, not a true
Q4_K_M: 6 threads, 512 prompt tokens, 128 generated tokens, three repeats. The serving run
cannot measure this, because the toy model answers in three tokens.

| Backend | Prompt, tokens/s | Generation, tokens/s |
|---------|-----------------:|---------------------:|
| CPU only (`-ngl 0`) | 368 ± 31 | 58.5 ± 9.1 |
| Metal | 3,956 ± 326 | 109.6 ± 11.7 |

These are the second run's (`logs/bench.log`). The measurement is noisy. The first run, read
from the terminal and not saved, gave 367 and 79 on the CPU and 2,795 and 110 with Metal. The
same command a few minutes after the second run gave 423 ± 53 and 76.7 ± 17.7 on the CPU
(`logs/bench-repeat.log`), with the machine's load average between 13 and 18 on 12 cores
(observed at the time, not saved). CPU
generation for the 0.5B model is somewhere between 59 and 79 tokens/s.

This is the 0.5B model on the Mac's own CPU, not in a container. The 7B model has about 15
times the parameters, and CPU speed scales roughly with size, so the Docker profile lands
about 4 to 5.3 tokens/s generating, on either side of the 5 tokens/s line of R19, and about 25
tokens/s reading the prompt, before the VM takes its share. Those are estimates, not
measurements: R19 stays open until the 7B file is benchmarked inside the container.

## What failed, and the fix

`mlx_lm fuse` stopped with `IncompleteSnapshotError`. It reads the base model's whole cached
snapshot with `local_files_only=True`, but `mlx_lm` downloads only the model files, so
`README.md` and `.gitattributes` are missing and `huggingface_hub` refuses the snapshot. One
call to `snapshot_download` for the base model before fusing completes it. The script does this.

Nothing else stopped the route. Qwen2's tokenizer and chat template convert without flags. Three
warnings are in the logs:

- Every server log: `control-looking token: 128247 '</s>' was not control-type; this is probably
  a bug in the model. its type will be overridden`. The f16 file still agrees with MLX on 10 of
  10, so it did not change these answers.
- `convert.log`: `Unknown RoPE type: default`.
- The quantize logs: the fallback warnings above.

## Not run

Route B (the unfused base as GGUF, the adapter converted with `convert_lora_to_gguf.py` and
loaded with `--lora`) was not run. Route A agrees at f16. At Q4_K_M it scored 8 of 10 in the
first run and 7 of 10 in the second, which under the plan's step 6 ("if A fails or disagrees")
would have triggered route B. Whether B would do better is not measured. It would first need
the adapter rewritten: MLX writes `adapters.safetensors` with its own config, and llama.cpp's
converter reads the PEFT layout. It remains the fallback if the real adapter loses answers at
the level that ships, or if a fused 7B model fails to convert.

Nothing in the spike was run in Docker. The CPU-only figures come from `-ngl 0` on macOS.

## Decision for week 3

**The rule was amended on 2026-10-02 (D12 in
[06-decisions-and-risks.md](06-decisions-and-risks.md)), for the GGUF export only.**
[01-constraints.md](01-constraints.md) 1.4 used to say: "Never download full-precision weights.
Never run `mlx_lm.fuse --dequantize` on this machine." The route is exactly the second step. The
spike ran `--dequantize` on the 0.5B model (about 1 GB). Downloading full-precision weights stays
forbidden, and so does every other use of `--dequantize`. The conditions under which the export
may run (rehearsal, disk preflight, stop rule and the rest) are in
[01-constraints.md](01-constraints.md) 1.4, "Conditions for the GGUF export". This document
records what each run measured; the conditions are not repeated here. What the route costs at 7B:

- **Disk, estimated:** about 15 GB for the dequantized directory, about 15 GB for the f16 GGUF
  (16-bit weights for 7.6 billion parameters) and about 5 GB for the quantized file. Kept side
  by side the three would be about 35 GB (15 + 15 + 5). Under the staged deletion of condition 6
  in 1.4 they never coexist: the dequantized directory is deleted before quantizing, so the
  peak is the dequantized directory plus the f16 GGUF, about 30 GB (15 + 15), while the f16 GGUF
  is written; quantizing needs the f16 GGUF plus the quantized file, about 20 GB (15 + 5). The
  preflight figure of condition 2 is therefore about 45 GB free (30 + the 15 GB floor). All
  of these are estimates; the rehearsal's measured sizes replace them here. `df` showed about 68
  GB free when measured on 2 October (it moves by a few GB as caches grow;
  `logs/df-2026-10-02.log`), which is above 45 GB at that reading. The bake-off's second
  candidate (up to 6.0 GB, 1.4) is deleted first under condition 3. Whether the bake-off's
  worst case of about 25 GB is already counted in that 68 GB reading is not known, so no
  figure is claimed for the two together; the `df` taken before the run decides.
- **Memory:** the peak of dequantizing and converting the 7B on 18 GB is not measured.

The route, as amended:

- **Route:** train the QLoRA adapter on the 4-bit MLX base as planned, then `fuse --dequantize`,
  `convert_hf_to_gguf.py` to f16, `llama-quantize`. The training itself does not change; the
  export adds the disk above and a rehearsal (below).
- **Pin** llama.cpp's binaries and its converter to the same build tag, and pin `mlx` as well as
  `mlx-lm`.
- **Complete the snapshot** before fusing.
- **Send `"cache_prompt": false`** in any comparison between runtimes.

Runs of the 7B export recorded here (condition 7 of 1.4): none yet.

Before and during week 3:

- **Benchmark first.** Run an off-the-shelf Qwen2.5-7B-Instruct Q4_K_M GGUF in the container
  before training anything. Speed does not depend on the adapter, and R19's mitigation, a
  smaller model, needs its own training run.
- **Rehearse the 7B route** with a throwaway adapter, and record peak memory, disk and any
  quantize fallbacks (condition 1 in [01-constraints.md](01-constraints.md) 1.4). Nothing is
  recorded yet.
- **Quantization level is chosen by measurement, not assumed.** After the real adapter is
  trained, compare MLX with Q4_K_M and Q5_K_M on at least 50 held-out labels. Those labels come
  from the validation part of the `train` split ([04-execution-phases.md](04-execution-phases.md)
  2.4), never from the holdout, `model_test` or `blind`.
- **Fix the acceptance rule in advance:** a margin threshold in log-odds, read on both runtimes,
  and task accuracy against gold, not only agreement with MLX. A change under the threshold is
  noise; a confident answer that changes is a reason to move up a level. `gguf_spike.py margins`
  shows how to read the margin from `llama-server`.
- **Narration** is judged by the grounding checker's pass rate per profile. Exact match means
  nothing for prose.
- **Tokenizers:** compare `llama-server`'s `/tokenize` with the Hugging Face tokenizer on Arabic
  labels.

## Open

- R19: generation speed of the 7B GGUF inside the container.

What the spike cannot have shown about the 7B:

- Memory and time for the 7B dequantize and convert.
- Safetensors split over several shards. The 0.5B model is one file.
- True K-quant levels. The toy files fell back to finer formats.
- Arabic. The toy task was English only, so neither agreement on Arabic labels nor tokenization
  parity between the two runtimes was checked.
- Long prompts and long generations. The prompts are under 40 tokens and the answers under 10.
- Anything in Docker or on Linux for the model.

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
anything else, or when the file or a port is missing. Compose passes a port to a container only
through `environment` and the port mapping, both from the same variable, so
`FRA_API_PORT=9000 docker compose up -d` (or `make docker-up`) maps, probes and listens on 9000.
The health path and the rule for "healthy" live in `fra_api.healthcheck`, which compose runs
inside the container and `make docker-health` runs from the host.

### Image

The image is a frozen sync of `uv.lock`, so it holds the locked, hash-checked versions.

| Piece | Value |
|-------|-------|
| Base | `python:3.12-slim` multi-stage image pinned by registry digest, non-root user; the `uv:0.8.3` tool image is pinned by digest too |
| mac extra | Not installed. The lock gives the `ocrmac` dependency the marker `sys_platform == 'darwin'`, so a Linux sync skips it and the pyobjc packages that only it pulls in; `import ocrmac` fails in the container |
| torch | 2.14.0+cpu and torchvision 0.29.0+cpu on Linux, 2.14.0 and 0.29.0 on the Mac: the same versions on both profiles. The root `pyproject.toml` pins both and sends them to PyTorch's CPU index on Linux |
| CUDA | Before this setting the lock held 19 packages the container cannot use (15 `nvidia-*`, 3 `cuda-*`, `triton`). It now holds none, and the image has none (checked: no installed distribution starts with `nvidia`, `cuda` or `triton`); `torch.cuda.is_available()` is `False` |
| Size | 1.68 GB, 1,679,494,773 bytes (`docker image inspect`); most of it is the virtual environment, 1,620,584 KiB by `du -sk` |
| Cold build | 148 s with `--no-cache` on the M3 Pro (Docker 27.3.1, aarch64, 12 CPUs, 14.1 GB for the VM per `docker info`), base images already pulled. The dependency layer is 90 s of it (wheel download 49 s, install 17 s, bytecode compile 18 s), and copying the environment into the final stage 29 s. A source-only change reuses the dependency layer |
| Start | `docker compose up -d --wait` returned in 6.6 to 6.9 s over three runs, with the healthcheck interval at 5 s |
| Stop | `docker compose down` with both services up takes 0.6 to 0.7 s; `docker compose stop worker` takes 0.3 s, because `init: true` forwards the stop signal to the sleeping process |

The build context is an allowlist (`.dockerignore`): the workspace sources, the lockfile and
`configs/`.

### Hosted Linux smoke evidence

On 2026-10-05, the [Container smoke run for source
`00063d681218365584e21a740694521a948e28d6`](https://github.com/noah-mclain/financial-reports-analysis-model/actions/runs/37326680288)
passed on the hosted Ubuntu 24.04 runner. Its Docker build installed Debian `amd64` packages,
so this verifies a Linux x86-64 build of the image. The smoke check imported `cv2`,
`docling.document_converter` and the FRA modules in the built image, checked the non-root
identity and CPU-only dependencies, started the API and placeholder worker, observed healthy
services and successful API health responses, then requested and observed bounded shutdown.

This is container build, import, health and lifecycle evidence only. The worker still only
imports `fra_ingest`, reports its profile and sleeps; this run did not exercise document
conversion, OCR accuracy, extraction, Gate D, model serving or production deployment. The run
does not replace the earlier aarch64 image-size, build-time or lifecycle measurements above.

### Deliberately missing

- No job queue, database or upload page (week 4).
- No `apps/worker` package; the worker service is a command in `compose.yaml`.
- No model in the image. The `llm` service waits for a GGUF file and was never started.
- No OCR engine in the image. The Apple Vision engine is Mac-only; the Linux engines arrive with
  week 2, and the image must grow them then.
- Hosted Ubuntu 24.04 CI built and smoke-tested the Linux x86-64 image on 2026-10-05 (see
  [hosted smoke evidence](#hosted-linux-smoke-evidence)). The size, cold-build time and local
  start/stop timings above remain aarch64 measurements; no corresponding x86-64 metrics were
  recorded.

### Left for later

- **Writable state.** `/app` is owned by root and `configs/ingest.toml` puts artifacts under
  `var/artifacts`, so the `app` user cannot write there until a volume owned by uid 1000 is
  mounted at `/app/var`, and another for the docling model cache.
- **Memory limits** for the services.
- **Pinned model image.** `llama.cpp:server` remains a floating tag; pin it by digest before
  the model service is used.
- **Healthcheck cadence.** Use `start_interval` with a longer `interval` once the API does real
  work.
- **Build cache.** A uv cache mount for the dependency layer.
- **Restart policy,** with the job queue.
- **OpenCV.** The lock has `opencv-python`, not the headless build. The slim runtime image
  installs `libxcb1`, `libgl1`, `libglib2.0-0`, `libsm6` and `libxext6`; the hosted x86-64 smoke
  run imported both `cv2` and `docling.document_converter` successfully in the built image.
  This verifies imports on that Linux architecture, not OCR behavior or accuracy.
- **Makefile, `.env` with CRLF line endings.** The error says only "LF line endings" and does
  not name Windows line endings (CRLF) as the cause.
- **Makefile, ports from the environment.** A port given in the environment or on the make
  command line is not validated (`FRA_API_PORT="80 80"` reaches the command); the whitelist
  covers the `.env` file only.
- **`scripts/spikes/gguf_route.sh`, port preflight.** The port is checked only at step 5, after
  training and conversion; a preflight next to the tool checks would fail sooner.
- **`scripts/spikes/gguf_route.sh`, shell details.** The INT and TERM trap is honoured only
  after the current foreground command returns. `grep -q` under `pipefail` could fail on a large
  writer (the writer gets SIGPIPE when `grep` exits early). A failing `llama-server --version`
  gives no message. The mlx-lm version string appears both in the script and in the Python usage
  docstring.
- **`scripts/spikes/gguf_spike.py`,** `compare()` has no automated test.
