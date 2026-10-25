# Prospective normalization control and treatment supplement

Status: design correction and diagnostic instrumentation only. This supplement is not an
execution authorization, replacement preregistration, frozen panel, model-selection result, or
promotion decision. No training or real-model generation was run under it. The existing
normalization runbook, receipts, preregistrations, configs, panels, reports, and evaluator remain
unchanged.

## Diagnosis

**Proven design flaw:** requiring the historical control to satisfy its stability thresholds
before measuring a treatment makes the experiment unable to test its hypothesis when that
control is already known to be unstable. A control efficacy failure is evidence about the
control and its eligibility for advancement; by itself, it is not a safety or integrity failure
and must not prevent collecting a bounded, matched treatment result. Control reproducibility,
treatment acceptance, safety aborts, and advancement are separate decisions below.

**No demonstrated aggregate numerical defect:** the supplied deterministic comparison reports
that the attempted reference matches historical D on all 35 recorded scientific training fields
(iteration, train loss, learning rate, trained tokens) and all three validation records. Its
whole training events differ only in runtime telemetry (throughput and peak-memory counters).
The historical D report records 60 updates, five validation points, the final adapter, 2,161
supervised tokens, and matching source/package identity and selection. The historical and
reviewed source hashes differ because the reviewed implementation makes optimizer and bias-pair
configuration explicit. The existing three-update real-AdamW CPU equivalence proof establishes
default-versus-explicit optimizer equivalence for that short check; it does not establish full
source identity or a complete replay. These aggregate facts do not identify which validation
rows caused D's peak, nor prove a causal optimizer explanation.

**Remaining hypotheses:** row-specific hard examples, source/target ambiguity, stochastic
training effects, and optimizer trajectory remain possible contributors. The smallest permitted
next observation is the prospective per-row validation trace below on the already fixed 64-row
selection, alongside the existing aggregate checks. It does not justify selecting new examples,
reconstructing the dataset, or opening a protected pool. The sample audit in the supplied local
evidence is not an independently rebuilt full-dataset audit.

## Evidence boundaries and historical comparator

Historical D is a reusable **exploratory efficacy comparator** for its recorded trajectory and
adapter because its report records a complete 60-update run, all five validation points, the
final adapter hash, and the same recorded selection and package environment as the reviewed
reference. The deterministic historical-to-reference comparison is a paired numerical
comparison of the specified recorded fields; it is not byte-identical source replay, a full
source-equivalence claim, or causal proof. Existing local evidence records historical source
hashes and optimizer config, adapter hashes, and the reviewed source identities. Remaining
source and runtime-context differences limit any stronger replay claim. Do not assume another
reference replay is required solely to make historical D usable for comparison.

The attempted reviewed-source reference stopped after 35 updates and three validations. Keep it
classified as partial and explicitly aborted. Do not overwrite, complete, or reclassify it, and
do not substitute it for historical D. Newly instrumented source will have different hashes
from the frozen source pins in the old preregistration and receipts. Preserve those pins and
receipts; any future authorized run requires a new prospective source binding and appropriate
owner authorization.

The legacy evaluator's `both_stable` result continues to mean that both arms passed the existing
stability rules. It is not reinterpreted as a paired numerical equivalence check. Do not alter
the evaluator or its tests to encode this supplement.

## Prospective decision sequence

1. **Control reproducibility and completeness.** Record whether the reference/control completed
   the exact frozen design and whether its evidence is reproducible against an available
   comparator. Report control stability independently. Historical D is already known to fail
   stability: its maximum validation loss was 1.5013533213844816 times its initial value, while
   final loss was lower. That failure means the control is unstable and non-promotable; it does
   not alone block measuring a matched treatment. Missing, partial, identity-mismatched, or
   nonfinite control evidence remains visibly incomplete or invalid and cannot support a
   reproducibility claim.
2. **Treatment measurement and acceptance.** A future treatment must use the fixed design and
   satisfy all treatment criteria below. A control stability failure does not waive or relax any
   treatment criterion. Report control and treatment metrics side by side; no winning arm is
   inferred from treatment success alone.
3. **Mandatory safety and integrity aborts.** Abort on quota/authentication error, identity,
   hash, version, or selection mismatch, preflight refusal, nonfinite loss, incomplete exposure,
   resource or runtime excess, protected-data need, or scope expansion. Preserve partial output.
   Do not retry, resume, repin, or promote a failed run. These stops apply regardless of control
   stability.
4. **Candidate advancement.** This supplement authorizes no Gate C decision, checkpoint, model
   test, blind-pool use, generation, adapter selection, export, or promotion. Even a treatment
   that satisfies the stated treatment criteria requires a separate owner decision and the
   existing downstream gates.

## Frozen treatment design and acceptance

The sole intervention remains AdamW bias correction `false` in the reference and `true` in the
treatment. Keep 60 updates, 240 fit examples, 2,161 supervised tokens, seed 17, learning rate
`1e-5`, true batch size 4, accumulation 1, 16 LoRA layers, rank 16, scale 20, dropout 0.05,
sequence cap 2,048, validation at updates 0/15/30/45/60, and final update 60 checkpoint. Keep
the same tokenizer, row selection/order, validation rows and row-mean aggregation. The treatment
must be finite, complete exactly 60 updates and exact exposure, final validation at most 1.10
times initial, every validation at most 1.25 times initial, and maximum train loss at most
5.142303466796875. Only bias correction is the optimizer intervention.

Keep the existing frozen representative and challenge panels, quality metrics, scoring
denominators, issuer bootstrap method and seed, minimum support, rare/critical-class rules,
validation budgets, and per-panel generation budgets in
`normalization-follow-up-protocol.md`. Report absent regional English/Arabic strata as **not
shown**. Keep critical safety unestablished at current panel support and retain the documented
label-conflict and independent-gold limitations. A successful treatment stability result is not
generated-quality evidence; no model generation is authorized here.

## Row-level validation diagnostics

The instrumented trace records each already-computed singleton validation loss, supervised
target-token count, evaluation update, and canonical target class or null. The row identifier is
the existing selected source index joined with the canonical row digest, preserving uniqueness
when identical row contents occur at distinct indices. It adds no prompt, source document, or
other row contents to event output. Aggregate validation remains `math.fsum(row_losses) / 64`,
the original arithmetic mean of per-row token-normalized losses, not a token-weighted mean.
Evaluation mode, restoration to training mode, padding, masking, row ordering, and train
objective remain unchanged. Logging uses values already computed by the existing pass and adds
no model call or random draw. Malformed provenance, duplicated identifiers, empty supervision,
token-count disagreement, or incomplete validation fails loudly; a nonfinite row is retained in
the partial event before the existing finite-loss stop.

No retrospective row losses are fabricated. Row diagnostics do not identify causality on their
own and do not authorize changing the selection or tuning against the validation targets.
