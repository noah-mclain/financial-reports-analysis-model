# Week 1 Carry-over: GGUF Spike and Docker Skeleton

**Goal:** Close the two week 1 items still open, in one day (Friday 2026-10-02), so week 2 starts with only its own work: prove that an MLX LoRA adapter can be fused and run as GGUF in llama.cpp (R16), and make `docker compose up` serve a health page.

**Why these two, and in this order:** The spike removes the largest unknown before week 3 trains anything. The skeleton is small and unblocks Gate D. Neither touches extraction.

**Spec:** `docs/blueprint/08-revised-plan.md` (Two run profiles, week 1 row, R16, R19), `docs/blueprint/01-constraints.md` 1.5 and 1.6.

## Measured before planning (2026-10-01, this Mac)

| Fact | Consequence |
|------|-------------|
| `docker` CLI is installed; the daemon is not running | Docker Desktop has to be started by hand before Task 2 |
| No `llama-server`, `llama-cli` or `ollama`; `brew` and `cmake` are present | `brew install llama.cpp` in Task 1 |
| `mlx`, `mlx_lm`, `fastapi`, `uvicorn` are not in the workspace environment; `transformers` is | The spike runs in a throwaway environment (`uv run --with`), so `uv.lock` does not change. Task 2 adds FastAPI and uvicorn properly |
| No model weights on disk apart from docling's; 106 GB free; 18 GB RAM | A small model is enough to prove the route, and costs minutes instead of an hour of downloads |
| `01-constraints.md` 1.5: `mlx_lm.fuse` exports GGUF only for Llama, Mistral and Mixtral | The route for Qwen is fuse and dequantize in MLX, then llama.cpp's own converter |

## Global constraints

- Work on the branch `week1-carryover`. One commit per task. `make test`, `make lint`, `make typecheck` and `make docs-check` pass before each commit, checked by exit code, not by reading output.
- Commits are authored as the repository owner, with no trailers (`CLAUDE.md`).
- Model weights, GGUF files and adapters stay under `var/` and are never committed.
- Each task is time-boxed. When a box runs out, record what was found and stop; do not borrow from the other task.
- One review pass at the end of the day, by reading the diff. No multi-pass review: the day's budget does not fit one.
- Nothing here runs against the `model_test` or `blind` pools.

---

### Task 1: GGUF spike (time box: 3 hours)

**Question to answer:** Can a LoRA adapter trained with MLX on a 4-bit Qwen2.5 model be fused, converted to GGUF and served by llama.cpp, giving the same answers as the MLX model? If not, where does it break, and what is the other route?

**Why a small model:** The route depends on the architecture, not the size. `Qwen2.5-0.5B-Instruct` is the same family as the planned `Qwen2.5-7B-Instruct`, downloads in a minute and converts in seconds. The 7B run is then only a matter of disk and time, and is left to week 3.

**Files:**
- Create: `scripts/spikes/gguf_route.sh` (the commands, runnable end to end), `docs/blueprint/14-run-profiles.md` (what was found)
- Work directory: `var/spikes/gguf/` (ignored)

**Steps:**

- [ ] **1. Tools.** `brew install llama.cpp`; confirm `llama-server --version` and `llama-quantize --help`. Clone llama.cpp at the same version into `var/spikes/gguf/llama.cpp` (shallow) for `convert_hf_to_gguf.py`.
- [ ] **2. A toy adapter.** With `uv run --with "mlx-lm[train]==0.31.3"`: write 40 chat examples to `var/spikes/gguf/data/` that map a line-item label to a canonical id (taken from `fra_core`'s taxonomy aliases, so the adapter does something checkable), and train a LoRA for about 100 iterations on `mlx-community/Qwen2.5-0.5B-Instruct-4bit`.
- [ ] **3. Reference answers.** Run 10 held-out labels through the MLX model with the adapter at temperature 0 and save the outputs.
- [ ] **4. Route A: fuse and dequantize.** `mlx_lm.fuse --dequantize` to a full-precision model directory; `convert_hf_to_gguf.py` to an f16 GGUF; `llama-quantize` to Q4_K_M.
- [ ] **5. Serve and compare.** Start `llama-server` on the Q4_K_M file, send the same 10 prompts through its OpenAI-compatible endpoint with the same chat template and temperature 0, and compare with step 3. Record tokens per second.
- [ ] **6. Route B, only if A fails or disagrees:** convert the unfused base model from Hugging Face to GGUF, convert the adapter with llama.cpp's `convert_lora_to_gguf.py`, and serve base plus adapter (`--lora`). Compare the same way.
- [ ] **7. Write it down.** In `14-run-profiles.md`: the commands that worked, agreement on the 10 prompts for each route tried, tokens per second, file sizes, what failed and why, and the decision for week 3. Put the working commands in `scripts/spikes/gguf_route.sh`.
- [ ] **8. Commit:** `Record the GGUF route for a fused MLX adapter`.

**Done when:** one route gives the same canonical id as MLX on at least 9 of the 10 prompts, or both routes are shown to fail with the reason written down. Either outcome closes R16 as a question; a failure opens a decision for the owner (see below).

**Stop and record if:** the tool install takes more than 30 minutes, training does not start, or neither route works after 3 hours.

---

### Task 2: Docker skeleton (time box: 3 hours)

**What the plan asks for:** a base image, a compose file, both profiles start, and `docker compose up` serves a health page.

**What is built now, and what is not:** an `api` service that really runs, a `worker` service on the same image that starts and reports ready, and an `llm` service defined but switched off by default, because it needs a model file that Task 1 only produces as a toy. No job queue, no database, no upload page: those are week 4.

**Files:**
- Create: `apps/api/pyproject.toml`, `apps/api/src/fra_api/__init__.py`, `apps/api/src/fra_api/main.py`, `apps/api/tests/test_health.py`
- Create: `docker/Dockerfile`, `compose.yaml`, `.dockerignore`
- Modify: root `pyproject.toml` (workspace member, test paths), `Makefile` (`dev`, `docker-up`, `docker-health`), `docs/blueprint/14-run-profiles.md`

**Steps:**

- [ ] **1. Owner, before starting:** open Docker Desktop and give the VM at least 6 GB for today (the plan's 10 to 12 GB is for the full pipeline later).
- [ ] **2. Test first.** `apps/api/tests/test_health.py`: `GET /health` returns 200 and JSON with `status: ok`, the package version and the run profile (`native` or `docker`, from the `FRA_PROFILE` environment variable); `GET /` returns an HTML page that names the profile. Run it and see it fail.
- [ ] **3. The app.** `fra_api.main:app` with FastAPI 0.141.1 and uvicorn, the two routes, nothing else. `make dev` runs it natively (`FRA_PROFILE=native`). Tests pass.
- [ ] **4. The image.** `docker/Dockerfile`: `python:3.12-slim`, `uv` copied in, the workspace installed without docling's GPU extras and without `ocrmac`, a non-root user, `FRA_PROFILE=docker`. Keep the first build honest about size and time: record both.
- [ ] **5. Compose.** `compose.yaml` with `api` (port 8000, healthcheck on `/health`), `worker` (same image; for now a command that imports `fra_ingest`, prints the profile and stays up), and `llm` (`ghcr.io/ggml-org/llama.cpp:server`, under a compose profile so it does not start without a model).
- [ ] **6. Prove it.** `docker compose up -d --build`, then `make docker-health` curls `/health` and checks the reply. `docker compose down`.
- [ ] **7. Write it down** in `14-run-profiles.md`: image size, build time, what each service does today and what it will do, and what is deliberately missing.
- [ ] **8. Commit:** `Add the API skeleton and a compose file that serves a health page`.

**Done when:** `make dev` and `docker compose up` each serve `/health` with their own profile name, and the fast tests include the API test.

**Stop and record if:** docling's dependencies will not install on the slim image inside the time box. Then the image ships without `fra_ingest` today, the worker service is left out, and that gap is written into `14-run-profiles.md` for week 2.

---

### Task 3: Close week 1 (30 minutes)

- [ ] Update `docs/exec-summaries/progress-2026-10-01.md`, or add a short dated note beside it: the two items, what was found, and week 1's "done when" row for Docker.
- [ ] Read the day's diff once, top to bottom, as the review.
- [ ] Push `week1-carryover`, write `var/pr/week1-carryover.md`, and open the pull request.

---

## Order of the day

| When | What |
|------|------|
| First | Start Docker Desktop (owner). Task 1, steps 1 and 2, since the downloads and the install run while reading |
| Morning | Task 1 to its done-when or its box |
| Afternoon | Task 2 |
| Last | Task 3 |

If only one task can finish, it is Task 1: a late Docker skeleton costs half a day in week 2, while an unknown GGUF route can cost week 3.

## Decisions that may come up

- **If no GGUF route works:** the Docker profile needs another way to run the model. The choices are to train on the full-precision base instead of the 4-bit one, to run narration in Docker on the base model without the adapter, or to make narration native-only and say so in the demo, as the plan already allows for scanned Arabic.
- **If tokens per second on CPU look far below 5** even for the small model: note it for R19; nothing to decide until the 7B model is measured.
