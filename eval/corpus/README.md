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
   must decline with a reason. Each carries `sector`, and other financial companies a `subsector`,
   so the industry signal can be scored (`docs/blueprint/09-ingest-locate.md`).
5. **`model_test` and `blind` freeze at the first model_test evaluation.** After that only
   `train` grows, and only to close a gap an evaluation measured (for example, too few Arabic
   examples of a critical item). Scores from different weeks stay comparable.

`make corpus-check` enforces rules 1 and 2 on `candidates.yaml`. `make corpus-fetch` enforces
rule 2 again on the downloaded bytes.

## Current size

Measured 2026-09-26 and 2026-09-27 (`fetched.yaml`): 224 documents from 142 issuers in Saudi
Arabia, Egypt, the UAE, Kuwait, Bahrain, the UK and the US, all downloaded and measured:
12,421 pages (181 mixed, 32 digital, 11 fully scanned). 4 byte-identical duplicates and 3
golden-issuer documents are flagged.

The set was fetched on two machines, and the 207 files measured on both have identical
sha256, so the URLs serve the same bytes regardless of where they are fetched from. Seven
hosts refuse automated clients (the Egyptian Exchange, Tawuniya, Tesco); those files were
saved from a browser to the paths `make corpus-fetch` prints, then measured.

| Pool | Issuers | Documents |
|------|---------|-----------|
| dev | 1 | 3 |
| train | 84 | 128 |
| model_test | 30 | 49 |
| blind | 30 | 49 |

Egypt: 45 documents from 21 issuers, 11 of them scanned or mostly scanned (3 in `blind`,
2 in `model_test`, 6 in `train`), on top of the 10 scanned filings in the golden set.

Blind top-up, 2026-09-27: five Arabic Egyptian Exchange filings from Talaat Moustafa, Golden
Tex Wool, Ferchem Misr and Gadwa, so the demo pool holds the hardest realistic case (Arabic,
likely scanned, Egyptian terminology). They are `verified: false` until fetched from a
browser and measured, which brings the total to 229 documents from 145 issuers.

English line-item volume comes from `make sec-fsds` (below), the largest source by far; its row
counts are recorded here after the first run.

## What the collection showed

These change the ingest design, not just the dataset:

1. **A text layer can be unusable.** Four Arabic filings carry a text layer whose font maps
   decode to Greek and symbol characters (`text_layer_undecodable`). Per-page detection must
   test whether the text is readable, not only whether it exists, and send such pages to OCR.
2. **pypdfium2 returns Arabic in visual order**, words reversed within the line. Labels need
   logical-order reconstruction before lexicon matching, or matching on reversed tokens.
3. **Scanned filings with an OCR text layer exist** (`ocr_text_layer`): the text reads
   `3I DECEMBER 2OT4`. Digits from such layers cannot be trusted without checks.
4. **Arabic header digits can come out reversed.** One filing's date extracts as
   `13 ديسمبر 9132`, the day 31 reversed and the year unreadable, so period parsing needs a
   cross-check (the auditor's report date, other statements) for Arabic headers.
5. **Scanned pages can carry both languages at once.** An Egypt Kuwait Holding results form puts
   Arabic and English labels side by side on one scanned page, with a stamp and signatures
   over the figures (`bilingual_page`).
6. **Many issuers publish both editions of the same statements.** Those pairs give aligned
   Arabic and English labels for training without hand annotation.

## Files

- `candidates.yaml`: what to collect. Hand-edited. Details come from search results until fetched.
- `fetched.yaml`: what was measured (sha256, pages, text layer per page, dedupe status).
  Written by the script.
- The PDFs themselves go to `var/corpus/<pool>/` and are never committed. The URL and sha256 are
  enough to reproduce the set.

## Model data

`make sec-fsds` (`training/sources/sec_fsds.py`) streams SEC Financial Statement Data Sets
into `training/data/sec_fsds/`: one row per distinct filer, statement, label and tag. It
applies the same split by filer (CIK), drops any CIK pinned to `blind` in `candidates.yaml`,
and keeps banks and insurers (SIC 6000 to 6499) as `negative_control`. It needs network
access to sec.gov and `FRA_SEC_USER_AGENT` set to a name and email.

The `train` documents supply Arabic and regional labels, paired with English ones through
issuers that publish both editions.
