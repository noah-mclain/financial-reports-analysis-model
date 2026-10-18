# Normalization validation comparison

Build a validation-only comparison before training. Reuse the approved dataset partition,
deduplication, rendering and label index. Preserve the existing train/validation files.

1. Test and implement scoring over immutable example and prediction records. Invalid replies
   remain errors. Report fixed-taxonomy macro-F1, abstentions, critical-item errors, issuer
   counts, source/language strata and annual/interim descriptions. Compare candidates with
   seeded paired issuer resampling; insufficient evidence never becomes a gate pass.
2. Test and implement validation reconstruction from approved inputs. Check registry and source
   metadata before labels, reproduce the partition manifest, align deduplicated rendered rows
   against the exact dataset bytes, and freeze target-independent sampling. Write a selection,
   lexicon predictions and report exclusively; refuse existing output.
3. Test the inference boundary and implement a local MLX runner using only label/statement
   prompts. Record model/tokenizer identity, generation settings, elapsed time and memory.
   Cache identical prompts within a run. No implicit download or scoring of other pools.
4. Run the focused tests, required repository checks and the real validation baseline. Document
   commands and measured limitations. Independent review precedes final commit or publication.

After this deliverable: bounded MLX training smoke; guarded 7B export rehearsal; measured QLoRA
schedule and validation comparison; candidate freeze and logged train-holdout evaluation;
runtime integration and parity. `model_test` and `blind` remain checkpoint-only. Narration
continues to use computed metrics and the existing grounding checker.
