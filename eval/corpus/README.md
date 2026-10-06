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
   documents. Nobody opens a holdout document to debug it; `make dry-run` (`eval/harness/dry_run.py`)
   reports ids, timings, outcomes and failure classes only, never a label or a value, so there is
   nothing to read. A dry run is entered in the scoring log like a model scoring and
   spends independence of the corpus part of the holdout; the SEC part is not touched, since
   SEC filers have no PDFs here. An issuer that has to leave the holdout, because a failure
   cannot be reproduced elsewhere, is moved to the fit part and recorded as a spent look. The
   limit is stated plainly: the holdout is "not individually tuned against", not "never seen",
   because `train` documents were already used in aggregate, and some individually. Which issuers
   those are, the overrides made for them and what the holdout holds (issuers, languages, period
   kinds, scanned and mixed files, pages without a text layer) are stated once, in
   `docs/blueprint/04-execution-phases.md` 2.4, and not repeated here.

`make corpus-check` enforces rules 1 and 2 on recorded identity/pool metadata. `make corpus-fetch` enforces
rule 2 again on the downloaded bytes.

## Current size

Measured 2026-09-26 to 2026-10-03 (`fetched.yaml`): 348 documents from 194 issuers in Saudi
Arabia, Egypt, the UAE, Kuwait, Bahrain, the UK and the US; 343 are downloaded and measured,
the five Egyptian Exchange filings of the blind top-up wait for a browser. The measured files
have 14,933 pages (222 mixed, 110 digital, 11 fully scanned). 4 byte-identical duplicates and 3
golden-issuer documents are flagged. By language, 170 documents are Arabic and 178 English;
by period, 229 are interim and 119 annual.

The first 224 files were fetched on two machines, and the 207 measured on both have identical
sha256, so the URLs serve the same bytes regardless of where they are fetched from. Seven
hosts refuse automated clients (the Egyptian Exchange, Tawuniya, Tesco); those files were
saved from a browser to the paths `make corpus-fetch` prints, then measured.

| Pool | Issuers | Documents | Issuers with Arabic | Issuers with both editions |
|------|---------|-----------|---------------------|----------------------------|
| dev | 1 | 3 | 1 | 0 |
| train | 118 | 208 | 87 | 46 |
| model_test | 36 | 65 | 25 | 14 |
| blind | 39 | 72 | 23 | 12 |

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
- Each company is compared with the existing issuers by name, by the listing's short name and by
  URL, so that nobody enters under a second name and lands in another pool (rule 1). 57 match an
  existing issuer exactly; 27 were decided to be an existing issuer under another name (25 nearly
  matched, 2 are renames that match nothing); 30 were decided different; 1 (Saudi Aramco) is left
  out as the parent of existing issuers; 89 are new. Matches keep the existing name, pool and role,
  and only the editions an issuer lacks were added; new issuers got `assign_pool`. The calls are in
  `argaam_decisions.yaml`. Translated or abbreviated names are caught only by a recorded decision
  or the URL check (below).
- 399 documents of 203 issuers were downloaded (two of them the Q1 English editions of issuers
  whose English file in the corpus is the Q2 one). A document is clean when no page has a
  garbled text layer; the text layer alone holds, for each of the statement of financial
  position and the statement of profit or loss, a page that the locator finds (no OCR) and that
  carries at least 20 numeric tokens, the two pages at most 2 apart; and the pages without a
  text layer before them are at most a one or two page auditor's letter right before the
  first. That first run kept a company only when both its editions were clean, the existing
  edition of an issuer included; the keep rule below replaced it. The rule checks that those
  pages carry text and figures; it does not check that they are the primary statements.
  The minimum of 20 numeric tokens was set from the located pages of the kept files (statements
  had 20 to 136, auditor's letters matched by the locator had 4 to 17) and one kept file sits
  exactly at 20, so a library or locator change that moves a token count can change the
  selection on a re-screen.
- 168 issuers (330 documents) are set aside in `deferred.yaml`, each with its reason: 157
  noisy (122 have no text-layer page for one or both statements, 30 a garbled text layer, 5
  located pages that carry too few figures, lie too far apart or follow images), 7 hold another
  period in the Q1 slot (4 a financial year that does not end in December, 3 a June 2026
  quarter) and 4 have an Arabic file that is not Arabic text (Halwani Bros. is counted among
  the noisy; only its English edition is still set aside, see the keep rule below). `collect`
  does not propose them again.
- Pool move, 2 October 2026: `al-sorayai-trading-and-industrial-group-na-ar` went from
  `model_test` to `train` under the issuer name Naseej, the new name of the same company
  (ticker 1213), because Naseej was already in `train` (rule 1).
  A machine that already holds the file under `var/corpus/model_test/` should move it to
  `var/corpus/train/` or fetch it again with
  `uv run python scripts/corpus.py fetch --id al-sorayai-trading-and-industrial-group-na-ar`.

Arabic on its own, and Nomu, 3 October 2026. The corpus held fewer Arabic than English
documents (136 against 161) and the owner wants cleanly written Arabic statements, so the keep
rule changed (`kept_editions` in `scripts/argaam_listing.py`): a company's clean Arabic edition
is kept even when its English edition is not clean; its English edition is kept only when the
Arabic edition is also clean, so new additions never widen the language gap; a company with no
clean Arabic edition is set aside as before. Companies with an Arabic edition and no English one
are collected too (the main market has one, Ladun Investment, whose Arabic Q1 statements are
not clean: found on pages 15 and 21). Re-screening the 8 set-aside companies whose reason did
not name their Arabic edition (16 documents, downloaded again) brought back one document, the
Arabic edition of Halwani Bros. (`train`); its English edition still fails, and the other 7
companies hold another period in the slot or have no clean Arabic edition.

The same listing exists for Nomu, the parallel market (`--market nomu`, market id 14; company
pages under `/en/tadawul/nomu/`, sector headings and table as on the main market). Nomu
companies report half-yearly, so the column read is Q2, the half year ended 30 June 2026
(the period check takes 30 June where the main market's takes 31 March). The listing held 119
companies: 118 with a Q2 edition (60 in both languages, 58 in Arabic only), the other a REIT
with a "First half" column. The funnel, in companies: 118 with an Arabic edition, 114 corporate
(4 financial services skipped), none golden; against all existing issuers of both markets, 2
matched exactly by name or URL, 3 were decided the same company under another spelling,
short name or language, 24 near matches were decided different, none left out, 85 had no near
match, which makes 5 existing issuers that gained editions and 109 new ones with `assign_pool`
(the funnel is recomputed with the corpus as it stood before the Nomu additions). 170 documents
were downloaded: 8 (5 companies) are set aside as wrong (2 companies show another period, 1 has
an Arabic file that is not Arabic text, 2 show no period the reader finds, a date it may not
read, which is not another period), 112 (76 companies)
are not clean, and 50 (33 companies) are kept: 33 Arabic and 17 English documents; 31 new issuers
(20 `train`, 6 `model_test`, 5 `blind`) and 2 existing (First Avenue Real Estate Development,
National Building and Marketing); 16 issuers with an Arabic edition only and 17 with both. The
documents are `period: interim`, `fiscal_year: 2026` like the main market's; `candidates.yaml`
has no field for the market, so a Nomu document differs from a main-market Q1 one only by the
period it shows and, where an id repeated, by the four-character suffix. The ones set aside
are in `deferred.yaml`. In all, the Arabic and English counts are now 170 and 178.

Limits: the new documents in `model_test` and `blind` were chosen for clean text layers, so those
pools lean cleaner than documents found at random, and a pool score on them will read better than
on the noisier filings the tool will meet. The documents are selected with the project's own
statement-title lexicon, so a statement titled in words it does not know is set aside rather than
kept. A parent and its consolidated subsidiary can sit in different pools: Seera Holding (`blind`)
and Lumi Rental (set aside, `model_test`); Emaar The Economic City (`model_test`) and Emaar
Properties (`blind`); SABIC (`train`) and Yansab (`model_test`); Savola (`blind`) and Herfy
(`train`); Saudi Printing and Packaging (set aside, `blind`) and Saudi Research and Media
(`train`); Halwani Bros (`train`, its Arabic edition only) and Aseer Trading (`blind`). Two more
group relationships are unverified, and the `model_test` company of each pair is set aside today:
Jamjoom Fashion Trading (`model_test`) with Jamjoom Pharmaceuticals (`train`), and Abdulaziz and
Mansour Ibrahim Albabtin (`model_test`) with Al Babtain Power and Telecommunications (`train`);
their pools must be settled before either is added. That is the owner's decision to take before the
checkpoint; nothing was moved. The Nomu documents are chosen the same way and lean cleaner too; a
file whose first five pages with text do not name the period is set aside as another period, which
can also mean a date the reader does not find. The Nomu issuers are those listed on 3 October 2026,
and two Nomu companies that share a brand with a main-market issuer were decided different because
they are listed side by side (Naseej for Technology and Naseej; Al-Modawat Specialized Medical and
Specialized Medical; each decided different, no group link between them found).

AME Company for Medical Supplies, a Nomu company, is the existing issuer Alf Meem Yaa Medical
Supplies (`blind`): the Nomu listing gives its Arabic name as ألف-ميم-ياء, the existing document's
URL ends in `IRAccessToken=AME` and the sector is the same, but the names share too little (score
0.29, under the 0.4 threshold) for a name match. It was first counted as a new issuer, its two
files were set aside under that name (pool `train` by hash), and now they are set aside under
Alf Meem Yaa Medical Supplies in `blind` by a `same` decision. `collect` now also treats an
existing issuer whose document URL carries `IRAccessToken=<short name>` as a near match that
needs a decision; on both listings it flags this one company and no other.

A period or language problem in either edition sets the whole company aside (`judge`), and
`names_later_period` reads Arabic as well as English ("المنتهية في 30 يونيو 2026"), so an Arabic
file that names a later period is no longer kept on its own. A re-screen of the kept documents
of both markets that are on disk (36 issuers, 69 documents on the main market; 33 issuers, 50
on Nomu) moved none. The reason of a period problem now says whether the reader found another
period (`another period is shown`) or none (`the reader found no period end in them`).

To reproduce, with a scratch directory `DIR` (it keeps the fetched pages, so a repeat run asks
the site for nothing it already has); `collect` without `--write` only reports, and run again
on the committed files it proposes nothing:

    uv run python scripts/argaam_listing.py collect --market main --cache DIR \
        --report DIR/near-matches.md --write
    make corpus-fetch NEW=1
    uv run python scripts/argaam_listing.py screen --market main --cache DIR --apply

Nomu is the same with `--market nomu` and its own scratch directory.

`screen` stops, naming the command, when a file it needs (also the existing edition of a pair)
is not on disk: `uv run python scripts/corpus.py fetch --id ID ...`.

`collect` stops, naming the company and the issuer, when one nearly matches an existing issuer (by
name, short name or URL token) and has no call in `argaam_decisions.yaml`. A listing sector that is
in neither list of the script stops it too.

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
- `argaam_decisions.yaml`: the calls on Argaam companies (both markets) that nearly match an
  existing issuer or were renamed (same, different with the issuers it was judged against, or
  exclude, with the reason). `collect` reads it.
- `deferred.yaml`: documents set aside, with the reason: noisy files, files that hold another
  period in the market's slot (Q1 on the main market, Q2 on Nomu), and files in the wrong
  language. Not in `candidates.yaml` or
  `fetched.yaml`; `collect` skips them, and their issuers keep the pool recorded there.
- `holdout_moves.yaml`: issuers moved out of the `train` holdout into fit, each with its reason
  and date (overrides before the first look, spent looks after). Hand-edited; the rules are in
  `docs/blueprint/04-execution-phases.md` 2.4 and the file format is read by
  `eval/harness/holdout_records.py`. `make corpus-split` prints the split; run directly,
  `scripts/corpus.py split` needs `PYTHONPATH=eval`.
- `validation_uses.yaml`: the validation documents that were read while an ingest rule was
  developed, with the date and the purpose. Hand-edited; the rule is in
  `docs/blueprint/04-execution-phases.md` 2.4 and the format is read by
  `eval/harness/holdout_records.py`.
- `scoring_log.tsv`: one row per look at the `train` holdout (dry runs and model scorings).
  Append-only, written before each scoring by `eval/harness/holdout_records.py` (`make dry-run` is
  the first writer); the rules are in `docs/blueprint/04-execution-phases.md` 2.4.
- The PDFs themselves go to `var/corpus/<pool>/` and are never committed. The URL and sha256 are
  enough to reproduce the set.

## Model data

`make sec-fsds` (`training/sources/sec_fsds.py`) streams SEC Financial Statement Data Sets
into `training/data/sec_fsds/`: one row per distinct filer, statement, label and tag. It
uses the shared `fra_core.pools` identity/assignment contract, excludes `blind` filers,
and keeps banks and insurers (SIC 6000 to 6499) as `negative_control`. It needs network
access to sec.gov and `FRA_SEC_USER_AGENT` set to a name and email.

The contract in `fra_core.pools` holds values and rules only. `scripts/pool_store.py` is the one
module that reads, writes and locks `var/issuer-pools.json` and reads the recorded YAML; the
corpus script, the Argaam listing and `sec_fsds.py` all go through it. Run directly,
`sec_fsds.py` needs `PYTHONPATH=scripts`; `make sec-fsds` sets it (`QUARTERS=8` for the latest
eight quarters, or `ONLY="2024q3 2024q4"` for those).

Recorded pools in `candidates.yaml`, `deferred.yaml`, fetched metadata and the local
`var/issuer-pools.json` are authoritative. Golden identities are always `dev`. Conflicting
records fail; a later pin cannot replace an earlier assignment. Identity links use normalized
names (the existing legal-suffix normalization), equivalent numeric CIKs, and explicit Argaam
rename/URL matches. Distinct CIKs under one normalized name require review. Unrelated names
without a CIK or an explicit recorded alias cannot be inferred to be the same issuer.

The two legacy defaults are preserved only for unseen identities: PDF names use 65% train,
20% model_test and 15% blind; SEC CIKs use 85% train and 15% model_test. The first recorded
assignment wins in either source order. SEC extraction registers its name and CIK before
writing any labels, including identities excluded as blind. PDF collection carries a known
CIK into entries for brand-new issuers so that the separate salted train subdivision can
match SEC rows. New editions of an existing issuer preserve its uniform identity metadata.
If its editions mix name-only and CIK keys, or a known CIK would need adding to old editions,
collection stops before writes: a coordinated metadata and salted split review is required.
Collection never rewrites existing candidates to add a CIK.
`collect --write` and `fetch` persist PDF assignments before writing their source artifacts.
A collection preview does not reserve new assignments. Metadata writers share a file lock
and sync the temporary file before replacement, then sync the containing directory. Keep
each checkout's registry with its corresponding data and back them up together; they are
local data, not tracked manifests. When transferring data to another checkout, transfer its
reviewed registry too; do not independently recreate it or share one mutable registry with
concurrent unrelated tasks. `corpus-check` also checks deferred and fetched pools and
cross-source recorded assignments, without opening labels or document contents.

**Historical SEC outputs without metadata.** Existing `labels-<quarter>.jsonl.gz` files must
have entries in the registry's `sec_outputs` list. Without that coverage, strict
`corpus-check` fails and PDF and SEC source commands stop before downloads or assignment
changes. The old outputs did not record their
allocation-time PDF pins separately, so applying today's pins or either hash is not a safe
migration. Recover identity/pool metadata from independently retained allocation-time records
and have it reviewed, including name/CIK aliases, the covered output names and any conflicts.
If such records do not exist, keep the outputs quarantined and stop cross-source additions
until a separately authorized reconciliation establishes their assignments. Do not inspect
held-out labels to debug, silently regenerate outputs, edit pins to pass a check, or create
an empty registry and mark historical files covered. This change does not migrate any data.
A failed SEC output write can leave reserved assignments and an output name in metadata;
retaining them is conservative, and a retry uses those same assignments.

**Reviewed registry recovery.** The sanctioned recovery input is a hand-authored JSON
registry, reviewed against independently retained allocation-time identity/pool records.
It is not reconstructed from label rows, held-out examples, today's pins or legacy hashes.
The version 1 schema below is a synthetic example, not production allocations:

```json
{
  "version": 1,
  "assignments": [
    {"names": ["acme"], "cik": 1111, "pool": "train"},
    {"names": ["acme"], "cik": 2222, "pool": "model_test"},
    {"names": ["regional widgets", "widgets brand"], "cik": null, "pool": "blind"}
  ],
  "sec_outputs": ["labels-2025q1.jsonl.gz"],
  "ambiguous_names": ["acme"]
}
```

`version` is the integer 1. Each assignment has a `names` list of normalized issuer keys,
a positive numeric CIK (integer or up to ten decimal digits), or `null` for a name-only
identity, and a pool of `dev`, `train`, `model_test` or `blind`. At least one usable name or
CIK is required; an empty `names` list is permitted for a CIK-only identity. Names within
an assignment are reviewed aliases. `sec_outputs` lists covered basenames exactly as
`labels-YYYYqN.jsonl.gz`, where N is 1 through 4. Coverage requires complete allocation-time
records of all issuers for those outputs, including excluded blind issuers. An empty registry
with covered output names does not constitute recovery. Legacy version 1 files may omit
`ambiguous_names`; omission means no declared ambiguity.

1. Stop source writers in the affected checkout. Retain backups of its existing registry
   and data. Inventory output basenames only; do not open labels or held-out documents.
2. Build a proposed registry outside tracked source from independently retained records,
   preserving every recorded pool, CIK and alias. Reconcile it with candidate, deferred,
   fetched and golden identity/pool metadata. A fetched id without a corresponding candidate
   or deferred identity needs restoration from reviewed records, not deletion to pass checks.
   A pool mismatch needs an owner-authorized reconciliation; a later pin cannot override it.
3. Have another reviewer verify identities, complete output coverage, pool preservation and
   any ambiguity against those records. If records are absent or conflict, keep the outputs
   quarantined and request an owner decision on a separate reconciliation task. Neither
   quarantine nor missing records permits bypassing the strict guard or silent regeneration.
4. With writers stopped, install the reviewed registry at `var/issuer-pools.json` in the
   checkout that owns the corresponding data. Run `make corpus-check`; resume additions only
   after it passes and the salted train identity metadata is consistent. Keep the review and
   allocation-time evidence with the private data backup, outside Git.

**Reviewed name ambiguity.** Normalized-name collisions reject by default and report both
CIKs. Only a reviewed normalized name in `ambiguous_names` stops being a linking key; its
SEC and PDF identities must then supply a CIK. The list contains unique, nonempty normalized
keys, not raw company names. In the example, both Acme CIKs retain their separate pools and
a PDF called Acme without a CIK fails. Usable, unlisted SEC names still undergo collision
checks; an empty or unusable SEC name can use its valid CIK alone.

Before declaring ambiguity in an existing registry, review the preserved CIK assignments
and every name-only record for that name. The loader refuses an ambiguous name-only
assignment, and source commands refuse an ambiguous name-only candidate, deferred or golden
identity. Assign those existing records explicit reviewed CIK identities together, preserving
their pools and aliases and reviewing the salted train split; do not drop or reassign them.
If a previous name-only group conflates different issuers, preserve all recorded pools in the
proposed disambiguation and reconcile any conflict separately before installing it. A reviewed
CIK-pinned assignment needs no pool change when its name is declared ambiguous.

The `train` documents supply Arabic and regional labels, paired with English ones through
issuers that publish both editions. The `train` pool is split by issuer into fit, validation
and holdout parts for the model (`docs/blueprint/04-execution-phases.md` 2.4, which holds the
counts and the overrides; the moves are recorded in `holdout_moves.yaml` and every look in
`scoring_log.tsv`; also how period kind, annual or interim, is recorded and reported,
D18); no `model_test` row will be written to the training data before the
checkpoint (the builder is not built yet).
