# Extraction week 1 summary

Oct 1, 2026 · @n

The week 1 extraction goal is met. Almarai's English and Arabic annual reports now come out as structured balance sheets, income statements and comprehensive income statements with identical figures, and every value traces to a page and box. Almarai, the only digital filing in the golden set, is fully clean, and the English Edita scans hold the balance-sheet identity. The Arabic and Juhayna scans are the weak spot: OCR loses too many balance-sheet numbers on Juhayna, and one Edita sheet has misread digits and its labels shifted against its values. That is the main problem week 2 must solve.

## Completed

All three ingest parts are built and reviewed. Parts 1 and 2 are merged; part 3a is pushed and awaiting merge.

| Piece | What it delivers | Status |
| --- | --- | --- |
| Ingest part 1: locate | Finds statement pages in English and Arabic filings, digital or scanned, and the page ranges to convert | Merged (PR #2) |
| Ingest part 2: convert | Converts only those ranges, in a separate child process per document, with OCR language and mode chosen per range | Merged (PR #3) |
| Ingest part 3a: structure | Turns the converted tables into statements, with periods read from header text, scale and currency, Arabic digit-order repair, pages continued across breaks, subtotal and balance-sheet checks, and a golden eval comparing language editions | Branch `ingest-structure`, pushed; PR text in `var/pr/ingest-structure.md` |

## Results

| Measure | Target | Result |
| --- | --- | --- |
| Almarai English vs Arabic, three statements | Identical figures | Met: 0 unmatched rows on either side for all three |
| Almarai scale, currency, balance-sheet identity | Correct | Met: thousands of SAR, identity holds |
| Golden documents converted without failure | 12 of 12 | 12 of 12 |
| Converter peak memory | Within budget | 3.19 GB at most, within the 3.5 GB budget (raised from 3.0 GB after measuring) |
| Conversion time per page | Recorded | About 2.5 s on digital pages, 4.5 s on scanned pages (median) |
| Balance sheets extracted, golden set | All 12 | 8 of 12; four scanned Juhayna filings yield none |
| Balance-sheet identity, golden set | Holds where values are present | Holds on 4 (Almarai EN and AR, Edita 2025 EN under EAS and under IFRS); skipped on 3 where totals were not found; fails on Edita 2024 AR. Both Edita EN sheets also fail a subtotal check, one on a real 20,000 misread, so "holds" there does not mean every figure is right |
| Fast tests | Pass | 585 pass; lint and type checks clean |

The structure eval exits FAIL for one reason, and that result is correct. On Edita 2024 AR, total assets and the printed total of equity and liabilities agree exactly for 2024 but differ by 5 for 2023: OCR read one digit differently in two cells that print the same figure. The same sheet has its labels shifted one row against their values, so the row labelled "total equity" holds the non-controlling interests figure. The statement is flagged as unsure, so the failure is visible, not hidden. A misread that breaks no check would still pass unseen.

## What the reviews caught

Every task was reviewed on its own, and the whole branch was reviewed again before the push. The final review found one serious error and seven smaller ones, all fixed:

- A comprehensive income statement was being merged into the income statement as if it continued it.
- An amount whose digits OCR ran together was read as the year 2077.
- Subtotal checks were skipped right under section headings, hiding real misreads.
- The eval gave credit for a wrong currency, and for a balance-sheet identity it never checked.
- Company names were read from page headers.
- Stale conversion results were trusted.
- A gated language pair with a missing statement was not reported as such.

A logic check of the code after that found four more mistakes, none of which had changed a golden figure, and they are fixed with tests:

- A continuation page that classification was unsure of could not borrow a missing column date from the page before, so it was dropped.
- A total whose values OCR lost entirely was read as a section heading, and the identity check then said the total was not found. It is now reported as values missing.
- Labels ending in "(IFRS 16)" or "Level 3" lost that number to the note column.
- A row equal to the row above it across a dash row was taken for a subtotal, leaving the real total unchecked.

Smaller hardening went in with them: per-share rows stay out of subtotal sums, merged figures far beyond their column are flagged, interim year-only headers keep their length, and currency detection now reads sterling, dollar signs, KD and SR.

## Next (week 2, Oct 3 to 9)

| Work | Done when |
| --- | --- |
| OCR engines behind one interface and a bake-off (Vision, Tesseract, RapidOCR), aimed at the Juhayna and Edita scans | Scanned balance sheets keep their numbers and the identity holds |
| Part 3b: row-alignment repair, a sanity check on figures, the review report | Edita 2024 AR passes or is held for review. Done 2026-10-01 on the same branch: it is held for review with the failing figure named, and `make eval-structure` passes. Results in `docs/blueprint/12-ingest-review.md` |
| Label mapping to the taxonomy, identity checks, about 12 metrics, declining banks and insurers | Gate A (extraction) and Gate B (mapping) on `dev` and `model_test` |

Not started from week 1: the Docker skeleton, and the spike that runs a fused MLX adapter as GGUF in llama.cpp. Both move into week 2.

## Part 3b, added 2026-10-01

Part 3b was built on `ingest-structure` the same day, so pull request #4 now covers 3a and 3b.

- Subtotals are scoped by the sums themselves: passing subtotal checks went from 139 to 184, and Almarai's and Edita IFRS's balance sheets are fully checked.
- The Edita 2024 AR row shift is repaired from the cell boxes. Total equity now reads 4,157,569,146 on its own label.
- A failed sum that is one digit off names its suspect. Edita's 2023 total assets is named at 7,743,342,651, which is what the page prints.
- Every statement is passed or held for review with reasons: 12 of 29 pass. `fra-ingest review-report <sha256>` draws every cell on its page image.
- A first comparison against the page, on three documents, gives 100% of 138 figures on Almarai EN, 95.24% of 126 on Edita IFRS and 77.34% of 128 on Edita 2024 AR. These are provisional: the expected files are drafts compared once, so Gate G1 is not measured.
- The scanned Arabic sheet is far below G1's 98%. Part 3b shows and holds its faults; reading it is the week 2 OCR work.

## Open decisions

- [x] Merge `ingest-structure` with the honest Edita failure recorded, or hold the merge for part 3b's row-alignment fix. Decided 2026-10-01: hold. Pull request #4 stays a draft until part 3b is done on the same branch, so it covers 3a and 3b. Design: `docs/blueprint/12-ingest-review.md`. Part 3b is done; the pull request stays a draft until the last changes on the branch are in.
- [ ] Give the three expected files in `eval/golden/expected/` their two readings against the page, or name who does, so Gate G1 can be measured.
- [x] Decide whether a figure the checks can name (Edita 2024 AR's 2023 total assets) stays flagged as read, or is replaced when two printed witnesses agree. Decided 2026-10-01: flag only (D10).
- [x] A statement with no printed scale: hold it, or use it. Decided 2026-10-01: use it as a warning, with a footnote on every amount that depends on it, through to the summary (D11). Plan: `docs/blueprint/13-unit-caveats.md`.
- [ ] Confirm the scale (units, thousands or millions) of the 10 Juhayna and Edita golden documents in `eval/golden/manifest.yaml`. Today scale is checked only on Almarai.
- [ ] Accept the recorded risk that two statements of the same type, printed on one page with the same layout, would be merged into one. There is no such case in the golden set.
- [ ] When to run the `model_test` checkpoint. Running it freezes the `model_test` and `blind` pools.

## Files to read

| File | Read it for |
| --- | --- |
| `docs/blueprint/11-ingest-structure.md` | The part 3a design, risks R29 to R35, and full results per document and per language pair |
| `docs/blueprint/12-ingest-review.md` | The part 3b design, results, and the provisional extraction figures |
| `docs/blueprint/10-ingest-convert.md` | Part 2 design, conversion times and memory, and the two decisions taken after the golden run |
| `docs/blueprint/08-revised-plan.md` | The schedule and gates A to E to the Oct 26 demo |
| `var/pr/ingest-structure.md` | The pull request text for part 3a |
