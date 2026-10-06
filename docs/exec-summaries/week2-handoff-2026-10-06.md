# Week 2 handoff: ready to merge, week 3 can start

Oct 6, 2026 · @n

A handoff note for the end of week 2 (3 to 9 October), written on day 11 of 31. It adds to [the closing note](week2-closing-2026-10-06.md), which still stands for what it measured, and corrects two statements (see "Two corrections"). Numbers marked Linux/Tesseract were measured today on a Linux machine (x86-64, 4 CPUs, Tesseract 5.3.4) under `FRA_PROFILE=docker FRA_OCR_ENGINE=tesseract`. Numbers marked Mac/Vision come from the owner's runs on the Mac and are quoted from the notes named beside them.

**Verdict: all seven week 2 tasks are built and reviewed, and everything is on one branch, `week2-integration-2`, ready for one pull request into `main`. Gates A and B are still not met, and the two Mac-only steps below have to be repeated before the numbers for `dev` can be trusted again. Week 3 can start now.**

## Week 2 tasks against the plan

The tasks are those of [2026-10-03-week2.md](../plans/2026-10-03-week2.md). "Built" means code and tests are on the branch. It does not mean the gate it serves is met.

| Task | Status | Evidence |
| --- | --- | --- |
| 1. Issuer split, override record, scoring log | Done. Fit 81 issuers / 144 documents, validation 15 / 29, holdout 22 / 35 | `scripts/corpus.py split`, re-run today |
| 2. First dry run on the holdout | Done twice: 4 October (3abdc22) and 6 October (7618503), 35 of 35 documents each | [closing note](week2-closing-2026-10-06.md) |
| 3. OCR bake-off | Done on 4 October. Docker uses Tesseract `ara+eng`; scanned Arabic is native-only. RapidOCR was cut, as the plan allowed | 14-run-profiles.md, "The OCR bake-off" |
| 4. Label mapping | Built (72b4508). Fuzzy matching, which the plan made conditional, was not built | `git log` |
| 5. Analytics | Built (05080b1): identity checks, metrics, unit caveats | `git log` |
| 6. Decline banks and insurers | Built (e37f2d5), then widened today. Its done-when is met on fit and not on validation (see "Measured today") | below |
| 7. Gates A and B on `dev` and the dry runs | Reported. Both gates NOT MET | [closing note](week2-closing-2026-10-06.md) |

## What is on week2-integration-2 and how to merge it

The branch is pushed. Its head before this note is 2a09a1f. It is `origin/dev-gates-repair` (bd7dc10) plus these merges, each made with `--no-ff`:

| Merge | Branch head | What it brings |
| --- | --- | --- |
| 1 | `critical-mapping-findings` c524065 | Unmapped critical items become named findings, no longer silent |
| 2 | `decline-other-financial` 136b3bf | Industry cues for consumer finance, asset managers and investment companies |
| 3 | `issuer-pool-consistency` d170c43 | Pool assignment kept the same across PDF and SEC sources |
| 4 | `rounding-tolerance-policy` 42dfb11 | One rounding rule for every check |
| 5 | `harness-ocr-failures` d223539 | An OCR timeout or engine error fails one document, not the run |
| 6 | `test-cache-isolation` 9fdf4ba | Golden tests run on a copy and never write under `var/artifacts` |

Merges 1 to 4 are the merge order in the plan, after `dev-gates-repair`. Merge 4 conflicted: `dev-gates-repair` and `rounding-tolerance-policy` had both replaced the scattered rounding constants, with two copies of the rule. The kept rule is `fra_core.tolerance`. `fra_ingest/check_tolerance.py` is deleted, and its three tests were folded into `packages/core/tests/test_tolerance.py`. No test was dropped.

**For the owner: open one pull request from `week2-integration-2` into `main`.** The other branches need no pull request of their own. Nothing here is pushed or opened by me.

Each branch was implemented and reviewed separately. All were approved with no Critical or Important finding open.

## Measured today

All lines are Linux/Tesseract unless marked.

| Check | Result | Source |
| --- | --- | --- |
| Fast suite, native profile | 2110 passed, 25 skipped | `pytest -m "not slow"` on the branch head |
| Fast suite, Docker profile | 2128 passed, 7 skipped | the same command under `FRA_PROFILE=docker` |
| `var/artifacts` before and after the suite | Byte-identical | the cache guard added by `test-cache-isolation` |
| ruff, mypy, `make docs-check`, `corpus.py check` | Exit 0 | run on the branch head |
| Fit negative controls (8) | 3 right / 5 held / 0 wrong, from 3 / 1 / 4 | rescored from cached pages |
| Fit corporates | 72 right and 1 held of 81 with a cache | the same rescore; 11 of the 92 fit PDFs present have no cache and are unmeasured |
| Validation negative controls (9) | 2 right / 5 held / 2 wrong | the same rescore |
| Golden documents recomputed with the new cues | 12 of 12 still pass as corporate; none declined or held | the same rescore |
| Golden conversion, Docker profile | 10 of 12 converted | the golden conversion run under the Docker profile |
| Golden Almarai and structure tests, Docker profile | 19 passed; 2 slow tests need Apple Vision and fail here by design | pytest, golden selection |

Three things the validation line needs said plainly:

- **The two wrong are both B Investments (2020 and 2023).** Its income statement page is scanned upside down and Tesseract reads mirrored text. No industry cue can fix that. It is a page orientation failure.
- **The investment-holding cue was tuned on validation.** The owner chose that, and it is recorded in `eval/corpus/validation_uses.yaml` (the 2026-10-06 entry, three investment-holding documents). Validation results for this sub-kind are therefore not an untuned check, until fit has investment-holding filings.
- **Coast Investment is held at exactly the 4.0 `near_threshold` boundary.** The score and the threshold are equal, so a small change in either flips it. The cue "management fees" was tried and rejected: it held Qalaa Holdings, a fit corporate.

Not comparable with Mac/Vision: in today's Docker-profile conversion, Tesseract found no statement pages in `juhayna-2024-ar-consolidated` and `juhayna-2025-ar-consolidated` (the locator reported no ranges). Vision on the Mac finds them. This fits the decision that scanned Arabic is native-only. The image's own convert run is still unmeasured (see the second correction).

Mac/Vision, from the owner's native run under LOCATE_VERSION 5 ([critical-mapping findings note](critical-mapping-findings-2026-10-06.md)): `dev` has 144 critical slots: 56 mapped, 9 ambiguous, 55 explicitly flagged, 0 silently unmapped, 24 in statements not found. Gate A is 138/138 digital cells, 218/254 scanned (85.83%), units 31/36 (30/36 in the closing note, before the header recovery of [dev-header-recovery-2026-10-06.md](dev-header-recovery-2026-10-06.md)). These were taken before `decline-other-financial` raised LOCATE_VERSION to 6, so they must be measured again.

## Two corrections

Both are checked against the files.

1. **The OCR bake-off was run.** The closing note says it "was not run as a real Tesseract/Docker comparison". `docs/blueprint/14-run-profiles.md`, "The OCR bake-off", records it on 2026-10-04 with a Docker row: Tesseract `ara+eng` in Docker read 114 of 126 English cells and 0 of 128 Arabic cells. The decision there is Tesseract `ara+eng` for Docker, with scanned Arabic native-only (R15, D14). The closing note is a dated record and is left as written; this note states the correction.
2. **The profile does switch the device.** `14-run-profiles.md` said "nothing switches it by profile". Since 7618503, `load_config` in `fra_ingest/config.py` sets `device = "cpu"` and the Tesseract engine when `FRA_PROFILE=docker`. That line of `14-run-profiles.md` is corrected in the same commit as this note. What stays unmeasured is the image's own convert run: the bake-off needed a copy of the settings, and no conversion has been timed in the image since the switch.

## Open failure classes

Each is fixed on fit, validation or `dev` material. None is debugged on a holdout document.

| Class | Size today | Where it will be fixed |
| --- | --- | --- |
| Scanned accuracy | 218/254 (85.83%) against a gate of 98.0% (Mac/Vision, `dev`) | `dev` and fit scans |
| Scanned Arabic on Tesseract | 0 of 128 cells; Juhayna not located (Linux/Tesseract) | Not fixed in Docker: native-only by decision. Show it held |
| Page orientation | 2 validation documents (B Investments) | Fit or validation pages that show the failure. No decline cue can fix it |
| Near-threshold industry boundary | Coast Investment at 4.0 | Investment-holding filings added to fit (week 3 start list), then re-score |
| Identity checks | 4 ok, 4 skipped, 3 without a balance sheet (Mac, `dev`) | `dev` |
| Scale and currency | 31/36 (Mac, `dev`) | `dev` |
| Mapped slots without a verdict | 36 (Mac, `dev`, LOCATE_VERSION 5) | `dev`, after the re-measure |
| Negative controls classed as corporate | Both holdout controls, in the second dry run | Re-check with LOCATE_VERSION 6 in the next dry run. Fix on fit and validation only |
| Decline rule unmeasured | 11 uncached fit documents | Rebuild their caches on the Mac |

Statements missing a critical item are now held as `needs_review`. More holds in the next dry run are by design.

## Minor notes deferred

- `fra_core/pools.py` brings file reading and `fcntl` locking back into core, against the direction of `dev-gates-repair`. Move load, save and lock to a scripts-side or training-side module.
- The locate all-fail guard costs up to 3 documents before it stops.
- `docs/blueprint/04-execution-phases.md` still names `corpus.issuer_key`.
- `GOLDEN_DIR` and the `FRA_PROFILE` default are repeated in the root `conftest.py`, `cache_isolation.py` and `packages/ingest/tests/support.py`.
- The stale-cache skip names the current settings, not the cached ones.
- `dry_run.py` is unchanged on purpose. Its failure class is now `ocr_timeout` or `ocr_engine` for the convert stage and `crash:OcrTimeoutError` for in-process stages, so earlier holdout records are not comparable on that field.

## Week 3 starts with

Week 3 is 10 to 16 October ([08-revised-plan.md](../blueprint/08-revised-plan.md)). Done when Gate C is provisional and normalization beats the baseline on held-out issuers.

1. Collect investment-holding filings for fit, on the Mac. The cloud cannot reach most filing hosts.
2. SEC FSDS and bilingual-pair datasets, with the issuer-grouped split and the length audit.
3. Baseline against QLoRA for normalization, on the train holdout.
4. The grounding checker, the templates, and narration in both languages.
5. GGUF export of the promoted adapter.

Dataset contracts, split tests, grounding and template tests can start on synthetic or fit inputs. Labels derived from extraction are not gold.

## Owner actions

1. Open one pull request, `week2-integration-2` into `main`.
2. On the Mac, with LOCATE_VERSION 6 and STRUCTURE_VERSION 19, re-run `make eval-convert`, `eval-structure`, `eval-gates` and `eval-mapping`, and refresh the `dev` numbers. LOCATE_VERSION 6 makes every earlier locate and conversion cache stale.
3. Collect the investment-holding filings for fit.
