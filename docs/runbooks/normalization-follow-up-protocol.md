# Normalization development quality and matched optimizer follow-up

This protocol prepares one prospective experiment. Training, real-model generation, export,
and protected-pool scoring have **not run** under it. Historical A/B/C/D all failed the
registered stability rules. D remains exploratory: its maximum validation loss exceeded the
allowed ceiling by 20.1083%. Successful B is `checkpoint-four-arm-final/B/report.json`, not
the failed supplement `runs/B/report.json`. Preserve both records.

Teacher-forced cross-entropy does not establish generated normalization quality. No adapter is
promoted by this preparation or by a successful CPU evaluator. The focused train/stability/eval
suite, including the real-optimizer CPU regression, has now completed successfully; see the
coordinator receipt in the private evidence file. Full repository checks and an independent
review remain pending. Execution is still blocked until those checks and review complete and the
owner separately authorizes resource use.

## Effective training contract

The inspected environment is MLX 0.32.2 / mlx-lm 0.31.3. `training_options` now explicitly passes
the existing AdamW defaults: bias correction **false**, betas `[0.9, 0.999]`, epsilon `1e-8`,
weight decay `0.01`. `fit` forwards these options and the configured learning rate to the real
`mlx.optimizers.AdamW` constructor. Decay multiplies parameters by `1-lr*weight_decay` before
Adam's moment update. Installed source evidence and hashes are in the private implementation
receipt. The focused CPU numerical regression compares three real optimizer updates at absolute
tolerance `1e-7`, relative tolerance zero, and passed. Its CPU fixture exercised the real
optimizer forwarding path without loading a real model. This focused result does not establish
that the full repository checks or independent review passed.

LoRA rank 16, scale 20 and dropout 0.05 are unchanged. In this installed implementation scale
is a direct output multiplier, not alpha divided by rank; B initializes to zero and A randomly.
There is no warmup, schedule, clipping, accumulation change, resume or adapter initialization.
`train_local` seeds NumPy with 17; `fit` seeds MLX with 17 before conversion. The diagnostic
selection permutes each split once with a fresh Python `Random(17)` and its batcher preserves
that recorded order. The ordinary smoke batcher length-sorts and shuffles; it is not substituted
for the diagnostic batcher.

Loss supervises shifted target positions `t >= offset` and `t < real_length`, excludes prompt
and padding, and sums FP32 cross-entropy divided by supervised tokens in each training batch.
At accumulation one, each microbatch is one update. Increasing accumulation would average
microbatch gradients, not weight them by supervised token count; this protocol forbids it.
Training reports every update. Diagnostic validation remains the arithmetic mean of 64
singleton row-normalized losses, with dropout disabled and training mode restored afterward.
It is deliberately unchanged; it differs from upstream token-weighted validation. Checkpoints
are cadence/final saves, not automatic best-loss selections.

## Authorized population and frozen panels

Private evidence root: `/private/tmp/normalization-quality-protocol/`. `freeze-panels.py`
checks the source roster, registry, recorded moves, approvals configuration and partition hashes
before reading an existing validation reconstruction. It checks every row's source id, CIK,
language and period against the authoritative validation roster, then matches every rendered
prompt/target to the pinned validation bytes. It never opens mixed source examples or protected
train-holdout/model_test/blind contents. This reuses the existing reconstruction's approved
partition identity; it does not claim independent PDF gold verification.

The current population is 139,271 validation rows from 938 issuers: SEC English annual 45,216,
interim 94,055; null 112,130, positive 27,141. Regional English and Arabic are absent and are
**not shown**. Current validation SHA-256 is
`ce2829c22420fdd4154a38c0c2789f03fdd96176a8dd5c61883544cdf4a781e5`.

Before any baseline comparison, `population-and-reconciliation.json` reconciles the historical
138,734-row population: 685 semantic-row additions and 148 removals, net 537. Additions are
206 null, 267 parent-net-income, 212 parent-equity rows; removals are 22 null, 4 total-equity,
119 depreciation/amortization and 3 net-income rows. Comparison preserves exact label,
statement, target and multiplicity, stripping only the changed system catalog. Roster, registry,
split and taxonomy hashes agree; source-example and approval-configuration hashes differ.
The frozen current exclusions affect fit only. These measured differences do not establish a
row-by-row causal relabel history. Historical baseline percentages are contextual evidence,
not matched current comparisons; regenerate the base on the frozen panels.

There are 70,562 distinct prompt inputs, 8,556 repeated-input groups, and 68,709 extra repeated
rows. There are 732 identical-input groups with different targets, comprising 20,224 rows.
An identical-input deterministic oracle has a row-accuracy ceiling of 97.6664% on this population;
this is a label-ambiguity diagnostic, not measured model performance or independent support.
The largest issuer has 488 rows (0.3504%); the ten largest together contribute 2.6617%.

Population critical support must distinguish rows, issuers and distinct prompt inputs:

| Class | Rows | Issuers | Distinct inputs |
| --- | ---: | ---: | ---: |
| revenue | 696 | 571 | 150 |
| net_income | 342 | 228 | 52 |
| total_assets | 748 | 740 | 11 |
| total_equity | 263 | 215 | 58 |
| total_current_assets | 684 | 678 | 14 |
| total_current_liabilities | 681 | 672 | 19 |

`panels-v1/representative-selection.json` contains 512 rows, 386 issuers and 484 distinct inputs.
It takes the lowest SHA-256 of `20261023:example_id`, ties by id, then outputs id order.
Selection uses neither labels nor predictions; repeated rows retain their population weight.
There are 350 interim and 162 annual rows. It contains only seven critical rows and no
`total_equity`; critical conclusions are not shown. It is a bounded population diagnostic,
chosen at the previously practical 512-row generation scale, not a powered rare-class estimate.

`panels-v1/challenge-selection.json` contains 168 rows, 148 issuers and 166 distinct inputs;
118 interim and 50 annual. It is separate from representative accuracy. Predeclared quotas,
in order: 16 per critical class; four per positive class with population support below 100;
32 ambiguous-input rows; 32 null targets. Ambiguous means the same exact system/user input
has multiple existing targets in permitted validation. Within each quota, traverse the same
seeded hash order and require a new issuer and input; exclude previously selected row ids.
The assets quota gets 11 of 16, current-assets 13 of 16; no replacement or lowered quota is
hidden. Later ambiguity rows add one assets row sharing an input, so aggregate assets support
is 12 rows but still 11 inputs. The other critical classes each contribute 16 rows/issuers;
none establishes safety. Four rare classes receive four rows each, also insufficient for a
class-quality claim. Full availability, exclusions by earlier quotas and limits are in the
challenge manifest. No labels or exclusions were changed.

Panels share zero row ids but 19 inputs. Neither shares row ids with the previous 64-row
diagnostic; representative/challenge share six/four distinct inputs with it. Both manifests
freeze ordered ids, hashes, seed/algorithm, quota shortfalls, issuer concentration, duplicates,
prior overlap, dataset/exclusion/taxonomy/prompt/tokenizer/base identities and source metadata.
Their selection fingerprints are respectively
`3aae0ba223e107ef11fb33892ced3c7627c09f082cec1b0ff6ba10e521c6d3d8` and
`b4b7f2f6338c2381fa97587676b34f5dcca2e8144876d1788c6607a7dfac8360`.
No unique-input score is substituted for the representative row-population score.

## Scoring and prospective development decisions

Reuse `normalization_validation.py score` and `normalization_eval.py`; no alternate scorer.
Score each panel separately. Base without adapter is the primary learned comparator; unchanged
`LabelIndex.match` is secondary. All three learned predictors use identical frozen rows,
system/user prompts, JSON schema, greedy temperature zero and a 64-token completion bound.
Only the final update-60 adapters are eligible. Generation caches identical inputs within each
panel/run; every original row and target remains in scoring.

Schema version 2 adds explicit count, denominator and rate fields without removing existing
scores. Empty denominators produce null, never zero-error evidence:

| Measure | Numerator | Denominator |
| --- | --- | --- |
| Exact accuracy | schema-valid canonical id exactly equals target, including null | all selected rows |
| Primary macro-F1 | mean per-id F1, unsupported ids contribute zero | all 47 canonical ids; null excluded |
| Macro-F1 with abstention | same, adding null | 48 classes |
| Invalid | malformed/extra-key/duplicate-key/unknown/wrong-statement reply | all rows |
| False mapping on null | valid non-null answer on null target | null-target rows |
| Unsafe null | any reply other than valid null, including invalid | null-target rows |
| Missed positive | valid null on non-null target | positive-target rows |
| Wrong non-null | valid non-null answer differs from positive target | positive-target rows |
| Positive error | missed, wrong non-null or invalid on positive | positive-target rows |
| Critical per class | any incorrect reply, including null/invalid | rows of that true class |

Invalid is a distinct error sentinel; it never becomes successful abstention. Missing, duplicate
or mismatched prediction ids fail loudly. Report per-class support, issuer and distinct-input
counts; binomial bounds assume independent rows and cannot certify repeated-input safety.
Critical summaries below 300 distinct inputs or 30 issuers say **not shown**; meeting those
counts alone would still not establish independence or Gate C. Bootstrap paired issuers at
least 1,000 times, seed 20261023, separately per source/language; strata below 30 issuers are
not shown. Do not pool SEC with regional outcomes or combine challenge with representative.

Prospective development go/no-go, fixed before generation:

1. Both fresh arms must satisfy the unchanged stability ceilings below, complete exact exposure,
   and stay inside resource bounds. Any incomplete/invalid run stops the experiment; no retry.
2. On representative SEC English, treatment-minus-base macro-F1's paired issuer-bootstrap
   two-sided 95% lower endpoint must exceed zero. Treatment accuracy must be at least base;
   invalid, unsafe-null and positive-error counts must not exceed base or matched reference.
   Treatment macro-F1 must be at least matched reference. These are development screens,
   not the formal +0.05 Gate C threshold.
3. On challenge, require at least eight distinct inputs and eight issuers for each critical
   class to interpret a directional diagnostic. Smaller support blocks that interpretation;
   one or two examples are never adequate. Require no new incorrect critical row, no new
   unsafe-null row, and no new invalid row relative to base or reference. Report rare and
   ambiguous subsets with their actual counts; four rare rows cannot support a class claim.
4. Every criterion must pass; missing evidence or unsupported source coverage blocks broader
   advancement. Even a pass authorizes only discussion of a separately scoped full-fit plan.
   Do not select a checkpoint or revise panels/thresholds after seeing results.

Formal Gate C is unchanged: per source, critical-error one-sided 95% upper bound at most 1%,
paired issuer bootstrap at least 1,000 with lower two-sided 95% adapter-minus-base macro-F1
endpoint at least +0.05, adequate issuer and independent critical support. Train-holdout scoring
requires separate authorization and a recorded look. D13/D16/D17 remain binding; model_test
and blind wait for the owner checkpoint. D21 keeps Arabic report-only.

## One matched mechanistic pair, not executed

The sole intervention is AdamW bias correction: reference false, treatment true. Corrected
moments alter early update magnitude; they are a hypothesis, not an assured remedy. Ignoring
epsilon on a nonzero first gradient, uncorrected Adam's step multiplier is about 3.162 times
its corrected counterpart. All other options, data order, seeds and aggregation are identical.

Both runs freshly load the same pinned Qwen2.5-7B 4-bit base revision
`c26a38f6a37d0a51b4e9a1eb3026530fa35d9fed`; neither loads a historical adapter. Historical D
cannot prove the new explicit optimizer plumbing or matched execution environment. Reference
is therefore replayed once alongside treatment. No sweep, second seed or fallback follows.

`pair-v1/preregistration.json` and its two configs freeze the pair and source hashes. Each arm:
60 updates, true batch four, accumulation one, 240 examples in the exact historical D order,
**2,161 supervised tokens**, LR `1e-5`, 16 adapted layers, rank 16, scale 20, dropout 0.05,
sequence cap 2,048 and seed 17. Fresh local tokenizer counting reproduced that exposure.
Together: 120 updates, 480 examples and 4,322 supervised tokens. Validation at 0/15/30/45/60
uses all 64 original diagnostic rows with the original row-mean reduction. Only final update
60 is selected; intermediate saves cannot be substituted.

Each arm must be finite, final validation at most 1.10 times initial, every validation at most
1.25 times initial, and maximum train loss at most 5.142303466796875, the unchanged historical
A comparison ceiling. The pair evaluator reuses the existing criteria and additionally refuses
unequal final supervised-token exposure. Historical rules, outputs and failure labels remain
unchanged. The final callback refuses token-budget drift before saving the final checkpoint.

Before any authorized execution, stop services and heavy workloads, require at least 15 GB
free disk and 14 GB available memory. Keep the existing 11.5 GB MLX training allocation and
1 GB cache caps. Train arms sequentially, at most 1,500 seconds each. Historical D took
1,227.77 seconds, peak MLX 8.1455 GB and RSS 4.0343 GB; these counters overlap and are not
added. The pair's 50-minute bound is a planning cap, not measured future runtime.

Generation is sequential after training stability passes: base, reference-final and
treatment-final, each on both panels. Maximum 2,040 scored rows, 1,950 generated completions
and 124,800 completion tokens (upper bounds, caching counted within each panel). Allow at most
1,500 seconds per representative process and 600 per challenge process: 6,300 seconds total.
Require peak MLX at most 6 GB and cache at most 1 GB; fail the phase on any exceeded counter.
Do not sum inference RSS and MLX or reuse training allocations as inference capacity evidence.
These generation budgets are prospective; current panels have not generated any responses.

Abort on quota/auth error, identity/hash/version drift, missing dependency, preflight refusal,
nonfinite loss, timeout, token/exposure mismatch, allocation excess, failed stability, unsafe
abstention regression, missing critical coverage or scope/protected-data need. Preserve every
partial run. Never resume, retry, silently repin or promote a failed candidate.

## Verified command shapes

The parser/help paths and synthetic config tests were checked without weights. The commands
below are **not executed** and require the preceding authorization and readiness conditions.
The private overarching protocol receipt freezes this document and all manifests. Do not
reuse output paths or rewrite preregistrations.

```sh
cd /Users/nadamohamed/.herdr/worktrees/financial-reports-analysis-model/normalization-quality-protocol
PY=/Users/nadamohamed/Developer/GitHub/financial-reports-analysis-model/.venv/bin/python
TASK=/private/tmp/normalization-quality-protocol
export PYTHONPATH=scripts:eval:training

# Sequential fresh-base fits; child deadlines, pins and allocations enforced by the runner.
/usr/bin/caffeinate -i "$PY" training/normalization_stability.py --config "$TASK/pair-v1/reference.json"
/usr/bin/caffeinate -i "$PY" training/normalization_stability.py --config "$TASK/pair-v1/treatment.json"
"$PY" training/normalization_stability.py --evaluate \
  "$TASK/pair-v1/runs/reference/report.json" "$TASK/pair-v1/runs/treatment/report.json" \
  --out "$TASK/pair-v1/evaluation.json"
```

Read `both_stable` in the evaluation; evaluator exit zero alone never means pass. Generation
requires both arms stable and a second readiness check. This future command uses the existing
runner, explicit subprocess deadlines and allocation controls, then checks its measured peak.
It stops at the first failure and refuses existing outputs:

```sh
/usr/bin/caffeinate -i "$PY" - "$TASK" <<'PY'
import json, os, subprocess, sys
from pathlib import Path
from normalization_train import probe_resources
root = Path(sys.argv[1])
config = json.loads((root / 'pair-v1/reference.json').read_bytes())
probe_resources().check()
out = root / 'generated-v1'
out.mkdir(mode=0o700, exist_ok=False)
# Set allocation before running the existing generation CLI in each isolated child.
child = ('import mlx.core as mx,runpy; mx.set_memory_limit(6_000_000_000); '
         'runpy.run_path("training/normalization_mlx.py",run_name="__main__")')
for predictor in ('base', 'reference', 'treatment'):
    for panel, seconds in (('representative', 1500), ('challenge', 600)):
        target = out / f'{predictor}-{panel}.json'
        command = [sys.executable, '-c', child, '--selection',
                   str(root / f'panels-v1/{panel}-selection.json'), '--model-dir',
                   config['model_dir'], '--model-manifest', config['model_manifest'],
                   '--out', str(target), '--max-tokens', '64']
        if predictor != 'base':
            command += ['--adapter-dir', str(root / f'pair-v1/runs/{predictor}/adapter')]
        subprocess.run(command, check=True, timeout=seconds,
                       env={**os.environ, 'HF_HUB_OFFLINE':'1', 'TRANSFORMERS_OFFLINE':'1'})
        run = json.loads(target.read_bytes())
        if run['metadata']['peak_mlx_bytes'] > 6_000_000_000:
            raise RuntimeError('generation allocation exceeded; preserve output as failed')
PY

# Repeat scoring for each panel/predictor; never concatenate panels.
"$PY" training/normalization_validation.py score \
  --selection "$TASK/panels-v1/representative-selection.json" \
  --predictions "$TASK/generated-v1/treatment-representative.json" \
  --base "$TASK/generated-v1/base-representative.json" --seed 20261023 \
  --out "$TASK/generated-v1/treatment-representative-score.json"
```

The secondary lexicon is the existing CPU-only `LabelIndex.match` path. After an authorized
generation run has produced the base predictions, these commands create new, exclusive outputs
for each frozen panel and score the lexicon with the existing CLI, paired against that panel's
base predictions. The commands are **not executed**. They fail if the output directory or any
output already exists.

```sh
cd /Users/nadamohamed/.herdr/worktrees/financial-reports-analysis-model/normalization-quality-protocol
PY=/Users/nadamohamed/Developer/GitHub/financial-reports-analysis-model/.venv/bin/python
TASK=/private/tmp/normalization-quality-protocol
export PYTHONPATH=scripts:eval:training
mkdir -m 700 "$TASK/lexicon-v1"

"$PY" - "$TASK" <<'PY'
import sys
from pathlib import Path
from exclusive_write import write_new
from normalization_validation import _json, lexicon_run, load_selection

root = Path(sys.argv[1])
out = root / "lexicon-v1"
files = {}
for panel in ("representative", "challenge"):
    selection = load_selection(root / f"panels-v1/{panel}-selection.json")
    run = lexicon_run(selection)
    files[out / f"{panel}-predictions.json"] = _json(run)
write_new(files)
PY

for panel in representative challenge; do
  "$PY" training/normalization_validation.py score \
    --selection "$TASK/panels-v1/${panel}-selection.json" \
    --predictions "$TASK/lexicon-v1/${panel}-predictions.json" \
    --base "$TASK/generated-v1/base-${panel}.json" --seed 20261023 \
    --out "$TASK/lexicon-v1/${panel}-score.json"
done
```

The existing `score` command validates selection fingerprints and prediction ids, then writes
the panel-specific lexicon scores and paired issuer-bootstrap comparison to unique output paths.
Keep representative and challenge reports separate. No legacy `prepare` command may reopen
mixed source examples to recreate these panels. Evaluate all listed development rules explicitly
and retain failures; no generated-quality outcome has yet been measured.
