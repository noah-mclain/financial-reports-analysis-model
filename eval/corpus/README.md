# Corpus: Pools and Split Rules

The golden set (`eval/golden/`) is for development. Everything beyond it lives here, split into
pools so that no score is measured on something the code or the model has already seen.

| Pool | Used for | Who opens it |
|------|----------|--------------|
| `dev` | Golden set issuers (Almarai, Juhayna, Edita). Debugging and development | Anyone, any time |
| `train` | Source documents for model training data. Split again by issuer into fit, validation and holdout parts (`docs/blueprint/04-execution-phases.md` 2.4); the holdout issuers are used for dry runs (rule 6) | The training data builders; the holdout only as rule 6 says |
| `model_test` | Scoring the model and the full pipeline | Only the eval harness, at the one checkpoint (D13, `docs/blueprint/06-decisions-and-risks.md`) |
| `blind` | Never used in development. Demo documents are picked from here | Only the blind run at the checkpoint (D13, D17), and the demo after it |

## Rules

1. **Split by issuer, not by document.** Every document from one issuer sits in one pool. A
   company's labels and layout carry over between years and languages, so splitting its
   documents across pools leaks answers (risk R7).
2. **Golden issuers are `dev`.** A newly found document from Almarai, Juhayna or Edita is
   marked `existing_issuer` and stays in `dev`, even for a different year. A file whose bytes
   match a golden document is marked `existing_document`.
3. **Nobody develops against `blind`.** No fixes aimed at one blind document, no reading its
   statements to tune a rule. There is no blind run before the checkpoint (D17); dry runs on
   the train holdout stand in for it (rule 6). When a dry run fails, record the failure class,
   then reproduce and fix it on a `dev`, fit or validation document with the same trait. If
   that is impossible, the issuer leaves the holdout (rule 6). When a blind run fails, at or
   after the checkpoint, record the failure with its class; nothing is moved or replaced,
   because `blind` is frozen (rule 5).
4. **Banks and insurers are `negative_control`.** They are in scope only as documents the tool
   must decline with a reason. Each carries `sector`, and other financial companies a `subsector`,
   so the industry signal can be scored (`docs/blueprint/09-ingest-locate.md`).
5. **`model_test` and `blind` freeze at the checkpoint, the first `model_test` evaluation.**
   It runs once, after all planned work is complete (D13), and the first blind run is part of
   it (D17). After that only `train` grows, and only to close a gap an evaluation measured
   (for example, too few Arabic examples of a critical item). Scores from different weeks stay
   comparable.
6. **Dry runs use the train holdout.** Until the checkpoint, the full pipeline is rehearsed on
   the documents of the corpus holdout issuers (the part of `train` that is never trained on,
   defined in `docs/blueprint/04-execution-phases.md` 2.4, which also holds the override rules).
   A dry run follows rule 3's procedure: run it, record the failure classes, fix them on other
   documents. Nobody opens a holdout document to debug it; the harness does not enforce this
   yet, so it is a rule only. A dry run is entered in the scoring log like a model scoring and
   spends independence of the corpus part of the holdout; the SEC part is not touched, since
   SEC filers have no PDFs here. An issuer that has to leave the holdout, because a failure
   cannot be reproduced elsewhere, is moved to the fit part and recorded as a spent look. The
   limit is stated plainly: the holdout is "not individually tuned against", not "never seen",
   because `train` documents were already used in aggregate, and some individually. Which issuers
   those are, the overrides made for them and what the holdout holds (issuers, languages, scanned
   and mixed files, pages without a text layer) are stated once, in
   `docs/blueprint/04-execution-phases.md` 2.4, and not repeated here.

`make corpus-check` enforces rules 1 and 2 on `candidates.yaml`. `make corpus-fetch` enforces
rule 2 again on the downloaded bytes.

## Current size

Measured 2026-09-26 to 2026-10-02 (`fetched.yaml`): 297 documents from 162 issuers in Saudi
Arabia, Egypt, the UAE, Kuwait, Bahrain, the UK and the US; 292 are downloaded and measured,
the five Egyptian Exchange filings of the blind top-up wait for a browser. The measured files
have 13,953 pages (202 mixed, 79 digital, 11 fully scanned). 4 byte-identical duplicates and 3
golden-issuer documents are flagged.

The first 224 files were fetched on two machines, and the 207 measured on both have identical
sha256, so the URLs serve the same bytes regardless of where they are fetched from. Seven
hosts refuse automated clients (the Egyptian Exchange, Tawuniya, Tesco); those files were
saved from a browser to the paths `make corpus-fetch` prints, then measured.

| Pool | Issuers | Documents | Issuers with Arabic | Issuers with both editions |
|------|---------|-----------|---------------------|----------------------------|
| dev | 1 | 3 | 1 | 0 |
| train | 97 | 177 | 66 | 37 |
| model_test | 30 | 52 | 19 | 8 |
| blind | 34 | 65 | 18 | 10 |

Egypt: 50 documents from 24 issuers, 45 of them measured (21 issuers); 11 of those are scanned or
mostly scanned (3 in `blind`, 2 in `model_test`, 6 in `train`), on top of the 10 scanned filings
in the golden set.

Blind top-up, 2026-09-27: five Arabic Egyptian Exchange filings from Talaat Moustafa, Golden
Tex Wool, Ferchem Misr and Gadwa, so the demo pool holds the hardest realistic case (Arabic,
likely scanned, Egyptian terminology). They are `verified: false` until fetched from a
browser and measured, which brought the total to 229 documents from 145 issuers.

Argaam interim pairs, 2 October 2026: 68 documents from 35 issuers, Arabic and English
editions of the same first-quarter statements (18 new issuers, 17 existing ones that gained
editions; two of those already held their English edition). `scripts/argaam_listing.py` reads
Argaam's financial statements listing for the Saudi market, 2026, the one year the site shows
without a subscription: 287 companies, 251 with both editions of Q1. The selection:

- Q1 2026 only, one pair per issuer, corporate issuers only. By Argaam's sector, banks (10),
  insurers (26) and financial services (10, financing and brokerage companies) are skipped;
  REITs and funds are on the skip list too, but none had both Q1 editions. A sector in neither
  list stops the run. Almarai is golden and skipped. 204 companies remained.
- Each company is compared with the existing issuers by name, by the listing's short name and
  by URL, so that nobody enters under a second name and lands in another pool (rule 1). 57
  match an existing issuer exactly; 27 were decided to be an existing issuer under another
  name (25 nearly matched, 2 are renames that match nothing); 30 were decided different; 1
  (Saudi Aramco) is left out as the parent of existing issuers; 89 are new. Matches keep the
  existing name, pool and role, and only the editions an issuer lacks were added; new issuers
  got `assign_pool`. The calls are in `argaam_decisions.yaml`. Renames are caught only by a
  shared URL or by a recorded decision.
- 399 documents of 203 issuers were downloaded (two of them the Q1 English editions of issuers
  whose English file in the corpus is the Q2 one). A document is clean when no page has a
  garbled text layer; the text layer alone holds, for each of the statement of financial
  position and the statement of profit or loss, a page that the locator finds (no OCR) and that
  carries at least 20 numeric tokens, the two pages at most 2 apart; and the pages without a
  text layer before them are at most a one or two page auditor's letter right before the
  first. A pair is kept when both editions are clean, the existing edition of an issuer
  included (the owner asked for cleanly written Arabic). The rule checks that those pages carry
  text and figures; it does not check that they are the primary statements.
  The minimum of 20 numeric tokens was set from the located pages of the kept files (statements
  had 20 to 136, auditor's letters matched by the locator had 4 to 17) and one kept file sits
  exactly at 20, so a library or locator change that moves a token count can change the
  selection on a re-screen.
- 168 issuers (331 documents) are set aside in `deferred.yaml`, each with its reason: 157
  noisy (122 have no text-layer page for one or both statements, 30 a garbled text layer, 5
  located pages that carry too few figures, lie too far apart or follow images), 7 hold another
  period in the Q1 slot (4 a financial year that does not end in December, 3 a June 2026
  quarter) and 4 have an Arabic file that is not Arabic text. `collect` does not propose them
  again.
- Pool move, 2 October 2026: `al-sorayai-trading-and-industrial-group-na-ar` went from
  `model_test` to `train` under the issuer name Naseej, the new name of the same company
  (ticker 1213), because Naseej was already in `train` (rule 1).
  A machine that already holds the file under `var/corpus/model_test/` should move it to
  `var/corpus/train/` or fetch it again with
  `uv run python scripts/corpus.py fetch --id al-sorayai-trading-and-industrial-group-na-ar`.

Limits: the new documents in `model_test` and `blind` were chosen for clean text layers, so
those pools lean cleaner than documents found at random, and a pool score on them will read
better than on the noisier filings the tool will meet. The documents are selected with the
project's own statement-title lexicon, so a statement titled in words it does not know is set
aside rather than kept. A parent and its consolidated subsidiary can sit in different pools:
Seera Holding (`blind`) and Lumi Rental (set aside, `model_test`); Emaar The Economic City
(`model_test`) and Emaar Properties (`blind`); SABIC (`train`) and Yansab (`model_test`); Savola
(`blind`) and Herfy (`train`); Saudi Printing and Packaging (set aside, `blind`) and Saudi
Research and Media (`train`); Halwani Bros (set aside, `train`) and Aseer Trading (`blind`). That
is the owner's decision to take before the checkpoint; nothing was moved.

To reproduce, with a scratch directory `DIR` (it keeps the fetched pages, so a repeat run asks
the site for nothing it already has); `collect` without `--write` only reports, and run again
on the committed files it proposes nothing:

    uv run python scripts/argaam_listing.py collect --cache DIR --report DIR/near-matches.md \
        --write
    make corpus-fetch NEW=1
    uv run python scripts/argaam_listing.py screen --cache DIR --apply

`screen` stops, naming the command, when a file it needs (also the existing edition of a pair)
is not on disk: `uv run python scripts/corpus.py fetch --id ID ...`.

`collect` stops, naming the company, when one nearly matches an existing issuer and has no call
in `argaam_decisions.yaml`.

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
- `argaam_decisions.yaml`: the calls on Argaam companies that nearly match an existing issuer
  or were renamed (same, different with the issuers it was judged against, or exclude, with the
  reason). `collect` reads it.
- `deferred.yaml`: documents set aside, with the reason: noisy files, files that hold another
  period in the Q1 slot, and files in the wrong language. Not in `candidates.yaml` or
  `fetched.yaml`; `collect` skips them, and their issuers keep the pool recorded there.
- The PDFs themselves go to `var/corpus/<pool>/` and are never committed. The URL and sha256 are
  enough to reproduce the set.

## Model data

`make sec-fsds` (`training/sources/sec_fsds.py`) streams SEC Financial Statement Data Sets
into `training/data/sec_fsds/`: one row per distinct filer, statement, label and tag. It
applies the same split by filer (CIK), drops any CIK pinned to `blind` in `candidates.yaml`,
and keeps banks and insurers (SIC 6000 to 6499) as `negative_control`. It needs network
access to sec.gov and `FRA_SEC_USER_AGENT` set to a name and email.

The `train` documents supply Arabic and regional labels, paired with English ones through
issuers that publish both editions. The `train` pool is split by issuer into fit, validation
and holdout parts for the model (`docs/blueprint/04-execution-phases.md` 2.4, which holds the
counts and the overrides); no `model_test` row will be written to the training data before the
checkpoint (the builder is not built yet).
