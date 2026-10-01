"""Helpers for the GGUF route spike (docs/blueprint/14-run-profiles.md).

    uv run python scripts/spikes/gguf_spike.py data
    uv run --with "mlx-lm[train]==0.31.3" python scripts/spikes/gguf_spike.py mlx \
        --adapter DIR --out FILE
    uv run python scripts/spikes/gguf_spike.py server --url http://127.0.0.1:8090 --out FILE
    uv run python scripts/spikes/gguf_spike.py compare REFERENCE CANDIDATE
    uv run python scripts/spikes/gguf_spike.py margins --url http://127.0.0.1:8090

`data` writes a toy task under var/spikes/gguf/data: map a line-item label to its canonical id.
The ten held-out labels are aliases the adapter never sees, for ids it does see, so an answer
is either the right id or it is not. `mlx` and `server` answer the held-out labels with the
same messages at temperature 0, and `compare` counts how many answers agree. `margins` prints,
for each held-out label, the step where the served model's first and second choice are closest:
an answer that flips between quantizations is only a finding if that step was not a near tie.

scripts/spikes/gguf_route.sh runs the whole route.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from pathlib import Path
from typing import Any

WORK = Path("var/spikes/gguf")
DATA = WORK / "data"
BASE_MODEL = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"
SYSTEM = "Map the financial statement line-item label to its canonical id. Reply with the id only."
HELD_OUT = 10
TRAIN = 40
MAX_TOKENS = 16


def messages(label: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": label}]


def write_data() -> None:
    from fra_core.taxonomy import load_taxonomy

    items = load_taxonomy().items
    held_items = [item for item in items if len(item.aliases_for("en")) >= 3][:HELD_OUT]
    held = [(item.aliases_for("en")[-1], item.id) for item in held_items]
    held_ids = {item.id for item in held_items}

    train = [(alias, item.id) for item in held_items for alias in item.aliases_for("en")[:-1]]
    for item in items:
        if len(train) >= TRAIN:
            break
        if item.id not in held_ids:
            train.append((item.aliases_for("en")[0], item.id))
    train = train[:TRAIN]

    DATA.mkdir(parents=True, exist_ok=True)
    _write_chat(DATA / "train.jsonl", train)
    # mlx_lm.lora wants a validation file; the toy task has nothing to tune, so it reuses
    # training rows and the loss it reports is not a held-out loss.
    _write_chat(DATA / "valid.jsonl", train[:5])
    (DATA / "heldout.jsonl").write_text(
        "".join(json.dumps({"label": label, "id": cid}) + "\n" for label, cid in held)
    )
    print(f"train {len(train)}, held out {len(held)} -> {DATA}")


def _write_chat(path: Path, rows: list[tuple[str, str]]) -> None:
    lines = [
        json.dumps({"messages": [*messages(label), {"role": "assistant", "content": cid}]})
        for label, cid in rows
    ]
    path.write_text("\n".join(lines) + "\n")


def held_out() -> list[dict[str, str]]:
    return [json.loads(line) for line in (DATA / "heldout.jsonl").read_text().splitlines()]


def answer_mlx(adapter: str | None, out: Path) -> None:
    from mlx_lm import generate, load  # type: ignore[import-not-found]

    model, tokenizer = load(BASE_MODEL, adapter_path=adapter)
    answers = []
    for row in held_out():
        prompt = tokenizer.apply_chat_template(
            messages(row["label"]), add_generation_prompt=True, tokenize=False
        )
        # mlx_lm's default sampler is greedy, which is temperature 0.
        text = generate(model, tokenizer, prompt=prompt, max_tokens=MAX_TOKENS)
        answers.append({**row, "answer": text.strip()})
    _save(out, {"backend": "mlx", "adapter": adapter, "answers": answers})


def answer_server(url: str, out: Path) -> None:
    answers = []
    for row in held_out():
        reply = _chat(url, messages(row["label"]), MAX_TOKENS)
        answers.append({**row, "answer": reply["choices"][0]["message"]["content"].strip()})

    _save(out, {"backend": "llama-server", "answers": answers})


def margins(url: str) -> None:
    for row in held_out():
        reply = _chat(url, messages(row["label"]), MAX_TOKENS, logprobs=True, top_logprobs=2)
        steps = [step["top_logprobs"] for step in reply["choices"][0]["logprobs"]["content"]]
        first, second = min(steps, key=lambda top: top[0]["logprob"] - top[1]["logprob"])[:2]
        print(
            f"{row['label']!r}: {first['token']!r} {math.exp(first['logprob']):.3f}"
            f" vs {second['token']!r} {math.exp(second['logprob']):.3f}"
        )


def _chat(url: str, msgs: list[dict[str, str]], max_tokens: int, **extra: Any) -> dict[str, Any]:
    payload = {"messages": msgs, "temperature": 0, "max_tokens": max_tokens, **extra}
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{url}/v1/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        reply: dict[str, Any] = json.load(response)
    return reply


def _save(out: Path, payload: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    correct = sum(a["answer"] == a["id"] for a in payload["answers"])
    print(f"{out}: {correct}/{len(payload['answers'])} equal the canonical id")


def compare(reference: Path, candidate: Path) -> int:
    ref = json.loads(reference.read_text())["answers"]
    cand = json.loads(candidate.read_text())["answers"]
    same = 0
    for r, c in zip(ref, cand, strict=True):
        agree = r["answer"] == c["answer"]
        same += agree
        mark = "same" if agree else "DIFF"
        print(f"{mark}  {r['label']!r}: {r['answer']!r} | {c['answer']!r}")
    print(f"agreement {same}/{len(ref)}")
    return 0 if same >= len(ref) - 1 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("data")
    mlx = sub.add_parser("mlx")
    mlx.add_argument("--adapter")
    mlx.add_argument("--out", type=Path, required=True)
    server = sub.add_parser("server")
    server.add_argument("--url", default="http://127.0.0.1:8090")
    server.add_argument("--out", type=Path, required=True)
    margin_parser = sub.add_parser("margins")
    margin_parser.add_argument("--url", default="http://127.0.0.1:8090")
    cmp_parser = sub.add_parser("compare")
    cmp_parser.add_argument("reference", type=Path)
    cmp_parser.add_argument("candidate", type=Path)
    args = parser.parse_args()

    if args.command == "data":
        write_data()
    elif args.command == "mlx":
        answer_mlx(args.adapter, args.out)
    elif args.command == "server":
        answer_server(args.url, args.out)
    elif args.command == "margins":
        margins(args.url)
    else:
        return compare(args.reference, args.candidate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
