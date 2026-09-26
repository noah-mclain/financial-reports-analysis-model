# Corpus: Pools and Split Rules

The golden set (`eval/golden/`) is for development. Everything beyond it lives here, split into
pools so that no score is measured on something the code or the model has already seen.

| Pool | Used for | Who opens it |
|------|----------|--------------|
| `dev` | Golden set issuers (Almarai, Juhayna, Edita). Debugging and development | Anyone, any time |
| `train` | Source documents for model training data | The training data builders |
| `model_test` | Scoring the model and the full pipeline | Only the eval harness, at checkpoints |
| `blind` | Never used in development. Demo documents are picked from here | Only the blind run at the end of each week, and the demo |

## Rules

1. **Split by issuer, not by document.** Every document from one issuer sits in one pool. A
   company's labels and layout carry over between years and languages, so splitting its
   documents across pools leaks answers (risk R7).
2. **Golden issuers are `dev`.** A newly found document from Almarai, Juhayna or Edita is
   marked `existing_issuer` and stays in `dev`, even for a different year. A file whose bytes
   match a golden document is marked `existing_document`.
3. **Nobody develops against `blind`.** No fixes aimed at one blind document, no reading its
   statements to tune a rule. When a blind run fails, record the failure class, then reproduce
   and fix it on a `dev` or `train` document with the same trait. If that is impossible,
   the document moves to `dev` and is replaced in `blind`.
4. **Banks and insurers are `negative_control`.** They are in scope only as documents the tool
   must decline with a reason.

`make corpus-check` enforces rules 1 and 2 on `candidates.yaml`. `make corpus-fetch` enforces
rule 2 again on the downloaded bytes.

## Files

- `candidates.yaml`: what to collect. Hand-edited. Details come from search results until fetched.
- `fetched.yaml`: what was measured (sha256, pages, text layer per page, dedupe status).
  Written by the script.
- The PDFs themselves go to `var/corpus/<pool>/` and are never committed. The URL and sha256 are
  enough to reproduce the set.

## Model data

The `train` documents supply Arabic and regional labels, paired with English ones through
issuers that publish both editions. English label volume comes from SEC Financial Statement
Data Sets (`training/sources/sec_fsds.py`), grouped by CIK. The same issuer-grouped split
applies there, and no SEC filer in `train` may appear in `model_test` or `blind`.
