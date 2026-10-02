# 06. Decisions, Risks and Repository Hygiene

## 6.1 Owner decisions

Most of these change numbers users see; D12 to D15 set how the work is run and what is
shown. Defaults below are implemented behind `configs/analytics.toml` so a decision can change
without code changes. Each decision needs
an owner sign-off before the Phase 1 exit gate.

| ID | Decision | Options | Recommended default |
|----|----------|---------|---------------------|
| D1 | Zero or negative denominators (negative equity for ROE, negative EBITDA for leverage, a loss year for cash conversion) | Return null with a flag, or compute with a flag | Margins: compute and flag `negative_base`. ROE, net debt to EBITDA, cash conversion: null with flag `undefined_negative_denominator` |
| D2 | Averages when the opening balance sheet is missing | Use the closing balance with a flag, or null | Closing balance with flag `average_fallback_to_closing` |
| D3 | IFRS 16 lease liabilities in total debt | Include or exclude | Include, with a UI toggle that recomputes |
| D4 | Day-count basis for DSO, DIO and DPO | 365 or 360; interim periods use actual days | 365; actual days for interim periods |
| D5 | Interim periods | Annualize flows, or skip flow-to-stock ratios | Skip in v1 with flag `interim_not_annualized` |
| D6 | Identity-check tolerance | Fixed absolute, relative, or rounding-aware | Rounding-aware: `n x 0.5` reported units, where n is the number of addends |
| D7 | Industry scope for v1 | All issuers, or non-financial corporates | Non-financial corporates. Banks, insurers and Islamic financial institutions route to `needs_review` with flag `unsupported_industry_template` |
| D8 | Narrative audience | Credit analyst, equity analyst, business owner | Credit analyst: liquidity and leverage first, neutral tone |
| D9 | Base model after the bake-off | Section 01, 1.6 | Decided by the rule in 1.6 |
| D10 | A figure the checks can name as misread (a failed sum one digit off, with one cell left as the suspect) | Replace the read figure with the one the checks give, or flag the cell and leave it as read | Decided 2026-10-01: flag only. The cell carries `digit_suspect`, the check names the figure that would settle it, and the statement is held for review ([12](12-ingest-review.md)) |
| D11 | A statement that prints no unit multiplier (`scale_missing`) | Hold it for review, or use it at scale 1 | Decided 2026-10-01: use it, as a warning and not a reason to hold, with a footnote wherever an amount depends on the assumption, carried through to the written summary ([13](13-unit-caveats.md)) |
| D12 | Running `mlx_lm.fuse --dequantize`, which 1.4 forbade, for the GGUF export of the 7B ([14](14-run-profiles.md)) | Keep the ban, or permit it for that export only | Decided 2026-10-02: permit it for the GGUF export only; the owner's words were "you can run it, but only very carefully". Downloading full-precision weights and every other use stay forbidden. The conditions (rehearsal, disk preflight, stop rule and others) are the engineering meaning given to "very carefully", not the owner's words, and are in [01-constraints.md](01-constraints.md) 1.4 |
| D13 | When the `model_test` checkpoint runs (it freezes the `model_test` and `blind` pools, `eval/corpus/README.md` rule 5) | During week 2 or 3, or once at the end | Decided 2026-10-02: "when we're 110% sure that everything is done and correct". Consequence as stated here: the checkpoint is taken once, after all planned work is complete and every check passes, not in weeks 2 or 3; until then Gates A and B are reported on `dev` only and marked "not yet measured on `model_test`" ([08](08-revised-plan.md), Gates) |
| D14 | Scanned Arabic filings that miss the scanned-page gate after the week 2 OCR bake-off | Show them as held for review, or cut them from the demo | Decided 2026-10-02: they stay in the demo, shown as held for review with their reasons; not cut, and not presented as correct ([08](08-revised-plan.md), R15). Not built yet: how such a filing is identified in the demo. The review step ([12](12-ingest-review.md)) holds a statement on failed checks and flags, and nothing there recognises a scanned Arabic filing that misses the gate |
| D15 | Two statements of one type on one page with the same periods and layout merged as one continuation (R35) | Refuse the merge, or accept it | Decided 2026-10-02, delegated by the owner: they must not be merged. A guard for balance sheets is built and tested on synthetic parts only; income statements are not covered and R35 stays open for that type. A low-confidence grid that would otherwise have continued a closed balance sheet is left unattached, with the reason recorded. The rest is in R35 of [11-ingest-structure.md](11-ingest-structure.md) |

## 6.2 Risk register

| ID | Risk | Impact | Likelihood | Mitigation | Owner work package |
|----|------|--------|------------|------------|--------------------|
| R1 | Scale misread ("in thousands" missed), making every value 1,000x wrong | Critical | Medium | Multiple signals (caption, header, page text, magnitude sanity check against sibling statements); conflicting signals block with `scale_conflict` | V4, T1 |
| R2 | Value columns shifted against period headers, usually around a "Notes" column | Critical | Medium | `header.py` detects note-reference columns; column x-ranges checked against header bboxes | V2 |
| R3 | Statement continues on the next page and rows are lost or duplicated | High | Medium | `continuation.py` merges by header signature and column geometry | V6 |
| R4 | Swap thrash when both heavy runtimes load at once on 18 GB | High | High without controls | Heavy lease, session profiles (dev unloads between jobs), `memory_guard`, preflight before demo and training runs | V10 |
| R5 | Disk exhaustion (44 GB free) | High | Medium | Section 01, 1.4 budget; `make disk-report`; LRU pruning of `var/store` | V10 |
| R6 | mlx-lm silently truncates long training examples | High | High | `length_audit.py` fails the build when any example exceeds `max_seq_length` | T10 |
| R7 | Train and test leakage because the same company appears across years | High | High | Group split by entity (CIK or registry id) before any other split | T10 |
| R8 | Fluent narrative with numbers that match no computed metric | Critical | Medium | Grounding checker, one regeneration, deterministic template fallback | V9, T8 |
| R9 | Upstream drift (docling releases often) | Medium | High | Exact pins in `uv.lock`; upgrades must pass `eval/thresholds.toml` gates | T11 |
| R10 | MLX or PyTorch MPS regression on macOS 27 | Medium | Low | Phase 0 smoke tests; pinned versions | V10 |
| R11 | Bank or insurer statements forced into the corporate template | High | Medium | Classifier detects financial-institution line items; D7 routing | V7 |
| R12 | Golden set documents with unclear usage rights | Medium | Low | Public regulatory filings only; source and retrieval date recorded in `eval/golden/manifest.yaml` | V1 |
| R13 | Arabic OCR quality, right-to-left column order, Hijri period headers | High (Phase 4) | High | Phase 4 spike before commitment; logical CSS properties and locale hooks in place from Phase 1 | Phase 4 |
| R14 | A demo or training run starts with other apps still open, causing swap and a slow customer-facing run | High | Medium | `fra-worker preflight` blocks `make demo` and `make train` below thresholds; demo runbook checklist; rehearsal in V10 | V10 |

## 6.3 Repository hygiene

1. **Own git root.** This directory currently sits untracked inside a parent repository.
   Phase 0 runs `git init` here and adds this directory to the parent's `.git/info/exclude`.
2. **Local state stays local.** Editor and cache directories go in `.git/info/exclude`, not
   `.gitignore`. The committed ignore file lists only project paths (`var/`, `.venv/`,
   `node_modules/`, training data and adapter weights).
3. **Audit.** V11 checks the above before every tagged release.
