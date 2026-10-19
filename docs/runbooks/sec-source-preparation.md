# SEC source preparation

Draft. It states what was established about the archives and the registry, the design of the
one routed pass that turns the quarterly archives into per-filing payloads for the
normalization data, and the pass as implemented. `num.txt` and the quarantined mixed-issuer
outputs are never opened. Stage 1 read only `sub.txt` (submission metadata, no labels), through
`training/sec_source_preparation.py`, and `readme.htm`, after the roster reader and the routing
were proved on synthetic archives (`tests/training/test_sec_source_preparation.py`).

## Facts

### (a) How SEC CIKs reach the pool registry

- `training/sources/sec_fsds.py` `extract` calls `pool_for`, which is
  `PoolRegistry.assign(Identity(name, cik), Source.SEC)`, for every submission of a supported
  form, then `main` saves the registry (`save_registry`) before it writes any output and adds
  `labels-<quarter>.jsonl.gz` to `sec_outputs`. A CIK with no recorded assignment is allocated
  there by hash: SHA-256 of `cik:<n>` modulo 100, below 15 is `model_test`, else `train`.
  A recorded assignment wins afterwards (`PoolRegistry.register` refuses a different pool for
  the same identity); the module has no way to remove or change one.
- `var/issuer-pools.json` (the registry file) was written on 2026-10-07 by that run. It lists
  `sec_outputs` for all eight archives (2024q3 to 2026q2). The mixed label files that run wrote
  are quarantined under `var/quarantine/sec_fsds-2026-10-07/` and are not opened.
  `var/sec-name-collisions-2026-10-07.txt` records 8272 distinct names, 22 of which link more
  than one CIK; the registry holds those 22 as `ambiguous_names`.
- Registry content, counted: 8235 assignments, of which 7856 carry a CIK (6649 train, 1206
  model_test, 1 blind) and 379 are name-only PDF issuers (these include the dev ones).
- The eight `sub.txt` rosters hold 53,158 submissions, 48,310 of a supported form (the
  annual and interim forms listed below) and 7856 distinct CIKs among those. Recorded: 7856.
  Unrecorded: 0. So routing needs no new pool allocation, and none was made: the routing code
  reads `registry.cik_pools()` and never calls `assign`.
- Recording an unrecorded filer would be a pool allocation: the hash decides model_test or
  train, and the result is permanent in effect. The reviewed path is the existing one
  (`sec_fsds.py` under the registry lock, or an explicit reviewed record). It is not needed here.

Commands (read only, run from the worktree):

```sh
uv run python <script using read_roster, registry_counts, route_filings>
```

where the script loads `var/issuer-pools.json` with `pool_store.load_registry`, the moves and
looks with `harness.holdout_records.read_moves` and `read_looks` (default repository files),
and the eight archives with `read_roster`.

Routing of the eight rosters (counts of submissions):

| Outcome | Submissions |
| --- | --- |
| routed to fit | 28,160 (4,590 CIKs; 8,167 annual, 19,993 interim) |
| routed to validation | 5,782 (957 CIKs; 1,684 annual, 4,098 interim) |
| dropped: holdout (unmoved) | 6,316 |
| dropped: pool model_test | 7,417 |
| dropped: pool blind | 7 |
| dropped: unsupported form | 4,848 |
| dropped: co-registrant not recorded train in the same part | 509 |
| dropped: one normalized name under two CIKs | 119 (32 CIKs) |

- Of 982 submissions with co-registrants (`aciks`), the dropped ones are counted above; the
  rest route.
- The four recorded moves (override) name PDF issuers that have no SEC filings. The shared
  split refuses a move whose issuer has no document, so a move is applied to the SEC roster
  only when some candidate carries that normalized name (`route_filings`). Today none does.
- One real archive row has an empty `period`, though the readme says it may not be null. It is
  kept as `None` and is not among the routed submissions.
- No accession repeats across the eight archives.

### (b) Column layout, from `readme.htm` (read only that member)

The files are UTF-8, tab-delimited, `\n`-terminated, with a first line of lowercase column
names (readme section 4). No quoting or escaping is documented, so a field never holds a tab.

| File | Columns, in the documented order |
| --- | --- |
| `sub.txt` | adsh, cik, name, sic, countryba, stprba, cityba, zipba, bas1, bas2, baph, countryma, stprma, cityma, zipma, mas1, mas2, countryinc, stprinc, ein, former, changed, afs, wksi, fye, form, period, fy, fp, filed, accepted, prevrpt, detail, instance, nciks, aciks |
| `tag.txt` | tag, version, custom, abstract, datatype, iord, crdr, tlabel, doc |
| `pre.txt` | adsh, report, line, stmt, inpth, rfile, tag, version, plabel, negating |

Meanings that matter here:

- `sub`: `adsh` is the key (`nnnnnnnnnn-nn-nnnnnn`); `cik`, `name` and `sic` are as of the
  filing; `form` is the submission type; `period` is the balance sheet date (`yyyymmdd`);
  `fy`/`fp` are the fiscal focus (`FY`, `Q1` to `Q4`); `nciks` counts the CIKs of the filing and
  `aciks` lists the additional (co-registrant) CIKs, space separated.
- `pre`: `stmt` is `BS`, `IS`, `CF`, `EQ`, `CI`, `SI` or `UN`; `report` and `line` order the
  rows; `inpth` is 1 for a value presented parenthetically; `version` is the taxonomy
  identifier for a standard tag and the `adsh` for a custom one; `plabel` is the text the
  filer printed; `negating` flags a negating label.
- `tag`: `custom` is 1 when `version` equals the `adsh`; the standard `version` is
  `<namespace>/<yyyy>`.

The reader checks the header by name for the columns it uses, not by position, so a reordered
or extended file still reads, and a missing column fails with member and line.

## Design

### The routed pass

1. Per archive, `read_roster` reads `sub.txt` only and `route_filings` returns the accession to
   part map and the counted drops (above). Nothing is allocated; a moves or looks record that
   fails the shared checks stops the run.
2. The one streaming pass reads `pre.txt` of each archive as bytes. For every line it takes only
   the text before the first tab and looks it up in the routed set. A routed line is parsed; any
   other line is counted and discarded without decoding its labels, never retained, printed or
   itemised. `num.txt` is never opened and `tag.txt` is not needed for the pass.
3. Routed rows are buffered per accession and written, per filing, as
   `<archive>/payloads/<adsh>.jsonl.gz` (canonical relative POSIX path) at the end of each archive. Rows keep `adsh`, `stmt`,
   `report`, `line`, `tag`, `version`, `plabel`, `negating` and `inpth`, and nothing else
   (`rfile` is dropped). The row count is declared in the artifact description.
4. A pass log, created exclusively, records: the owner's authorization reference, start and end
   times, the configuration hash, the registry (file and metadata), moves (file, applicable
   moves and split-records) and looks hashes, the routing drops and routed counts, and per
   archive its SHA-256, the `pre.txt` lines read, kept and discarded, the routed filings, the
   payload and output bytes, the accessions of routed filings with no rows (`empty_payloads`,
   also printed) and the index and manifest hashes. Payload hashes are in each archive's
   manifest, not in the log. Once written the log is not edited.
5. A malformed line inside a routed filing (wrong field count, a flag other than 0 or 1, a
   non-number, invalid UTF-8) aborts the pass. An empty or undocumented `stmt` does not: the
   decoder drops it as `unsupported_statement_code`. The error and
   `pass-abort.json` name the archive, the accession and the line number, never the line. An
   unrouted line is never parsed, so it cannot abort the pass.

### Records for `sec_source_access`

- `SecFiling.source_record` is a `SourceRecord`: `source_id` is the `adsh`; `issuer` is
  `Identity(name, cik)`; `pool` is train; `source` is SEC; `language` is `en`; `period` is
  `annual` for 10-K, 10-K/A, 20-F, 40-F and `interim` for 10-Q and 10-Q/A
  (`ANNUAL_FORMS` and `INTERIM_FORMS`, the single definition); `period_end` is the `period`
  date; `source_sha256` is the archive's SHA-256; `config_sha256` hashes the preparation
  configuration. `raw_locator` is `<archive name>:<adsh>`.
- One `SecIndex`, one `SourceManifest` (state `complete`, written after every artifact) and one
  `AccessContext` per archive, because a contract index covers one archive. The context's
  hashes are the ones printed by the pass log; the owner establishes them independently.
- The index lists the routed submissions only. The contract requires an artifact for every
  index filing and recomputes the split over the index, so a holdout, model_test or blind
  filing can have no entry: its payload is never written. Name-collision submissions are left
  out because the shared split refuses a normalized name under two CIKs. The moves handed to the
  contract are the applicable ones only, because it refuses a move whose issuer is absent from
  the roster; the pass log holds the full moves file hash, the applicable subset and the
  registry hashes. A name that normalizes to nothing is left out of the issuer identity, as
  the registry does (`issuer_identity`).
- The opener checks a trusted root, refuses a symlink or a non-regular file, and returns the
  artifact bytes for `iter_payloads`, which checks the declared hash.

### The decoder

For each verified payload it validates the schema of every row, that every `adsh` equals the
filing, the declared row count, and that no row repeats a `report`/`line`. Per row it then
yields a decoded row or a counted drop:

| Drop reason | Rule |
| --- | --- |
| `unsupported_statement_code` | `stmt` is not `BS`, `IS`, `CI`, `CF` or `EQ` (so `SI`, `UN`) |
| `unsupported_namespace` | a standard tag whose namespace is not the mapping's (`ifrs`, `us-gaap-ebp`) |
| `ambiguous_semantics` | the tag is under `excluded` in the mapping |
| `contradictory_presentation` | parent and companion lines in one report have the same label once casefolded and reduced to letters and digits; only lines that themselves resolve as the companion's total count; also a parent line whose label contains `noncontrolling`, `minority`, `consolidated`, the word `NCI`, the Arabic noncontrolling or minority terms, or is the bare "total equity" (the line is, or includes, the noncontrolling share) |
| `parenthetical` | `inpth` is 1: a parenthetical note, not a line item |
| `empty_label` | the label is empty after whitespace normalization |

A custom tag (`version` equals the `adsh`) gets namespace `custom` and tag taxonomy version
`extension`. Every other row becomes a `TagKey` and goes to `resolve_tag` with the crosswalk,
the policies, the role assessment for the filing and the trust snapshot. The result is a mapped
target, a null from an entry, or a policy abstention; unresolved is an error to count and look
at, never a null. The decoded row becomes a `NormalizationExample` with `months` `None`.

### Coverage report

Counts only, in total: rows by resolution kind (mapped, null entry, outside the crosswalk,
custom) and rows dropped by each reason above, the outside-crosswalk rows split into unlisted
balance-sheet and income-statement tags, listed tags without an entry and the statements the
taxonomy does not model, and the top N standard tags that no mapping row lists, by row count,
separately for `BS` and `IS`. The negative-control override needs the role assessments and is not
part of this report. The reviewer audits the crosswalk gaps from the last list before approving, so a frequent
unlisted tag is mapped, nulled or excluded on evidence.

## The months rule

`NormalizationExample.months` is now `None` for every SEC row, because a presentation row names
a line and the filing's columns carry the durations. PDF rows keep requiring months for flow
statements and forbid it for balance sheets (`training/dataset_contracts.py`, with regression
tests in `tests/training/test_dataset_contracts.py`).

## Decisions taken

1. Name collisions: the 119 submissions are dropped with the counted reason `name_collision`.
2. Role assessments: one reviewed rule table, not per-filing hand review (specified below,
   built in stage 2b).
3. Parenthetical rows (`inpth` = 1) are dropped as `parenthetical`.
4. A malformed line inside a routed filing aborts the pass.
5. The pass log records the full moves file hash, the applicable moves and the registry hash.

### Role assessment rule table (stage 2b, built; see "Roles")

`resolve_tag` needs an approved `RoleAssessment` per `source_id` and forbids a SIC fallback. The
unit is one reviewed rule table: SIC 6000 to 6499 is `negative_control`, any other present SIC
is `corporate`, and a missing or malformed SIC is `unknown`. A generator writes one
`RoleAssessment` per filing from the SIC in `sub.txt`, with review evidence naming the rule
table hash and the filing. The reviewer approves the rule table. The generator's output is
checked mechanically by recomputing every assessment from the roster and comparing hashes.
Tooling then issues the approval record of each assessment under that one verdict, with
`evidence_sha256` the hash of the rule table plus the roster-derived assessment list.
`training/sources/sec_fsds.py` `role_for` differs (a non-numeric SIC is corporate there), so
it is not reused; the rule table is the single definition.

## Commands

```sh
make sec-prepare ARCHIVES="var/sec_fsds/2024q3.zip ..." OUT_DIR=var/sec_prepared \
    AUTHORIZATION="<reference to the owner's authorization>"
make sec-coverage PREPARED=var/sec_prepared CROSSWALK=<draft>/crosswalk.json \
    ARCHIVES="var/sec_fsds/2024q3.zip ..." OUT=<report.json> [TOP=40]
```

Both take an optional `REGISTRY` path for a registry file outside the checkout. `OUT_DIR` must
not exist. `sec-coverage` reads only the prepared payloads, through the access contract, and
`tag.txt` for definitions. A completed run leaves `pass-log.json`; a failed one leaves
`pass-abort.json` (no line content) and the directory is not reused.

## The run on the eight archives

Authorized by the owner on 2026-10-18 as one logged, hash-recorded routed pass. The first
attempt stopped after four seconds, in `2024q3`, at a routed line whose `stmt` is empty (the
readme does not allow an empty `stmt`). Nothing had been written beyond the abort record,
which was kept as `var/sec_prepared.aborted-attempt-1/pass-abort.json`. The writer no longer
validates `stmt`; the decoder drops an empty or undocumented one as
`unsupported_statement_code`. The second attempt completed.

Measured from the completed run (`var/sec_prepared/pass-log.json`):

| Quantity | Value |
| --- | --- |
| wall time (`make sec-prepare`, including the access-contract self-check of every archive) | 22 min 51 s |
| payload bytes (gzip, 33,942 files) | 104,313,078 (the log of this run does not record the size of the index, manifest and context files; later runs record `output_bytes`) |
| submissions read from `sub.txt` | 53,158 |
| routed | 28,160 fit, 5,782 validation |
| dropped | holdout 6,316; pool model_test 7,417; pool blind 7; unsupported form 4,848; co-registrant not routed 509; name collision 119 |
| `pre.txt` lines read | 5,963,723 |
| rows kept (routed filings) | 3,742,460 |
| lines discarded unparsed | 2,221,263 |
| moves applied | 0 (the four recorded moves name issuers with no SEC filings) |

The registry, moves and scoring-log files hashed the same at the start and the end of the
run, and the log records those hashes in full.
The self-check calls `authorize_payloads` with every artifact of an archive before the archive
is written as complete; it dominates the wall time because the contract rebuilds the registry
snapshot once per filing.

## Coverage of the prepared payloads

Measured by `make sec-coverage` over the prepared payloads against the draft crosswalk (mapping
hash in the draft's own output), in 18 min 51 s. The report with each tag's `tag.txt` definition is
`var/sec_prepared/coverage-report.json`; it holds counts and definitions, no label.

| Resolution of the 3,742,460 rows | Rows |
| --- | --- |
| mapped by the crosswalk | 772,247 |
| null entry in the crosswalk | 102,535 |
| standard tag with no crosswalk entry (outside-crosswalk policy) | 2,026,980 |
| custom tag (custom / extension scope) | 370,216 |
| dropped: parenthetical | 270,744 |
| dropped: ambiguous semantics (excluded tag) | 61,983 |
| dropped: unsupported namespace | 82,489 |
| dropped: unsupported statement code (includes empty) | 54,161 |
| dropped: empty label | 1,105 |

These are rows before deduplication per issuer and before the negative-control override, which
needs the role assessments (stage 2b). The most frequent standard tags that no mapping row lists,
by rows, within the balance sheet and the income statement (top 40 each):

| Rank | Balance sheet tag | Rows |
| --- | --- | --- |
| 1 | `CommitmentsAndContingencies` | 22,960 |
| 2 | `PreferredStockValue` | 17,781 |
| 3 | `AdditionalPaidInCapitalCommonStock` | 10,557 |
| 4 | `DeferredIncomeTaxLiabilitiesNet` | 9,145 |
| 5 | `DeferredIncomeTaxAssetsNet` | 8,081 |
| 6 | `ContractWithCustomerLiabilityCurrent` | 7,486 |
| 7 | `OtherAssets` | 5,323 |
| 8 | `TreasuryStockCommonValue` | 5,132 |
| 9 | `EmployeeRelatedLiabilitiesCurrent` | 4,840 |
| 10 | `TreasuryStockValue` | 4,532 |
| 11 | `OtherLiabilities` | 3,337 |
| 12 | `AccruedIncomeTaxesCurrent` | 2,905 |
| 13 | `ContractWithCustomerLiabilityNoncurrent` | 2,708 |
| 14 | `TemporaryEquityCarryingAmountAttributableToParent` | 2,472 |
| 15 | `RestrictedCashCurrent` | 2,421 |
| 16 | `OtherReceivablesNetCurrent` | 2,391 |
| 17 | `AccruedLiabilitiesAndOtherLiabilities` | 2,276 |
| 18 | `TaxesPayableCurrent` | 2,200 |
| 19 | `Deposits` | 2,106 |
| 20 | `DeferredRevenueCurrent` | 2,020 |
| 21 | `InterestReceivable` | 1,992 |
| 22 | `RestrictedCashNoncurrent` | 1,792 |
| 23 | `IncomeTaxesReceivable` | 1,758 |
| 24 | `OtherAccruedLiabilitiesCurrent` | 1,703 |
| 25 | `AccountsPayableAndAccruedLiabilitiesCurrentAndNoncurrent` | 1,696 |
| 26 | `InterestPayableCurrent` | 1,676 |
| 27 | `DerivativeLiabilitiesCurrent` | 1,619 |
| 28 | `FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLoss` | 1,613 |
| 29 | `RestrictedCash` | 1,608 |
| 30 | `InterestPayableCurrentAndNoncurrent` | 1,501 |
| 31 | `Investments` | 1,495 |
| 32 | `InterestBearingDepositsInBanks` | 1,475 |
| 33 | `AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment` | 1,469 |
| 34 | `ContractWithCustomerAssetNetCurrent` | 1,466 |
| 35 | `LongTermNotesPayable` | 1,465 |
| 36 | `LongTermDebtAndCapitalLeaseObligationsCurrent` | 1,437 |
| 37 | `AccountsPayableAndOtherAccruedLiabilitiesCurrent` | 1,422 |
| 38 | `DebtSecuritiesAvailableForSaleExcludingAccruedInterest` | 1,420 |
| 39 | `AssetsOfDisposalGroupIncludingDiscontinuedOperationCurrent` | 1,397 |
| 40 | `BankOwnedLifeInsurance` | 1,369 |

| Rank | Income statement tag | Rows |
| --- | --- | --- |
| 1 | `WeightedAverageNumberOfSharesOutstandingBasic` | 26,565 |
| 2 | `WeightedAverageNumberOfDilutedSharesOutstanding` | 25,803 |
| 3 | `OperatingExpenses` | 15,718 |
| 4 | `OtherNonoperatingIncomeExpense` | 15,217 |
| 5 | `NonoperatingIncomeExpense` | 12,767 |
| 6 | `ResearchAndDevelopmentExpense` | 10,615 |
| 7 | `ComprehensiveIncomeNetOfTax` | 7,445 |
| 8 | `CostsAndExpenses` | 6,217 |
| 9 | `ProfessionalFees` | 4,288 |
| 10 | `GainsLossesOnExtinguishmentOfDebt` | 3,874 |
| 11 | `IncomeLossFromEquityMethodInvestments` | 3,815 |
| 12 | `LaborAndRelatedExpense` | 3,768 |
| 13 | `InterestIncomeExpenseNonoperatingNet` | 3,464 |
| 14 | `AmortizationOfIntangibleAssets` | 2,749 |
| 15 | `InterestExpenseOperating` | 2,737 |
| 16 | `OtherIncome` | 2,457 |
| 17 | `OtherComprehensiveIncomeLossForeignCurrencyTransactionAndTranslationAdjustmentNetOfTax` | 2,444 |
| 18 | `ForeignCurrencyTransactionGainLossBeforeTax` | 2,411 |
| 19 | `InterestAndDividendIncomeOperating` | 2,313 |
| 20 | `NoninterestExpense` | 2,232 |
| 21 | `NoninterestIncome` | 2,217 |
| 22 | `OccupancyNet` | 2,130 |
| 23 | `InterestIncomeExpenseAfterProvisionForLoanLoss` | 2,089 |
| 24 | `OtherOperatingIncomeExpenseNet` | 2,085 |
| 25 | `FairValueAdjustmentOfWarrants` | 2,032 |
| 26 | `OtherNoninterestExpense` | 1,965 |
| 27 | `RestructuringCharges` | 1,949 |
| 28 | `OperatingCostsAndExpenses` | 1,915 |
| 29 | `InterestAndFeeIncomeLoansAndLeases` | 1,862 |
| 30 | `InterestExpenseDeposits` | 1,750 |
| 31 | `ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest` | 1,703 |
| 32 | `CommonStockDividendsPerShareDeclared` | 1,672 |
| 33 | `OtherNonoperatingIncome` | 1,644 |
| 34 | `OtherCostAndExpenseOperating` | 1,633 |
| 35 | `GainLossOnInvestments` | 1,617 |
| 36 | `IncomeLossFromContinuingOperations` | 1,589 |
| 37 | `PreferredStockDividendsIncomeStatementImpact` | 1,534 |
| 38 | `NetInvestmentIncome` | 1,527 |
| 39 | `AssetImpairmentCharges` | 1,466 |
| 40 | `OtherComprehensiveIncomeForeignCurrencyTransactionAndTranslationAdjustmentNetOfTaxPortionAttributableToParent` | 1,460 |

The reviewer audits these before approving the crosswalk. Some are close to a taxonomy item and
need a mapping or an explicit null (for example `OtherAssets`, `OtherLiabilities`,
`OperatingExpenses`); others have no taxonomy item (share counts, deposits, comprehensive income).

## Authorization cost and the extended coverage

`authorize_payloads` built the registry's metadata (`PoolRegistry.metadata()`, which rebuilds and
sorts every assignment) after registering each filing, to compare it with the reviewed
snapshot. The probe itself was built once per call, as documented; the per-filing comparison
was the cost. It now checks each filing against an index of the snapshot's CIK, pool and names
built once per call. The probe still registers every filing, so conflicts raise as before, and a
filing whose CIK, pool or name is not in the snapshot is still refused ("not fully recorded").
Measured on the real `2024q3` roster (4,249 filings, authorize only): 135.1 s before, 0.6 s
after. `make sec-coverage` over all eight archives took 18 min 51 s before and 36 s after.

The coverage run with the full ranking (`UNLISTED=...`) wrote
`var/sec_prepared/coverage-report-2.json` and `var/sec_prepared/unlisted-tags.jsonl`, leaving the
first report in place. The kind and drop counts equal the first report. New figures:

| Outside-crosswalk rows (2,026,980) | Rows |
| --- | --- |
| balance sheet or income statement, tag not listed in the mapping | 618,283 |
| balance sheet or income statement, listed tag, version with no entry | 0 |
| comprehensive income, cash flow or equity (no taxonomy items) | 1,408,697 |

- Distinct unlisted standard tags: 1,330 in the balance sheet and 1,277 in the income statement
  (2,607 in `unlisted-tags.jsonl`, one line per statement and tag, most frequent first, with
  the tag's `tlabel` and `doc` in each tag taxonomy version of the vocabulary).
- Tags needed to cover a share of the 618,283 unlisted rows: 50% needs 57 tags, 80% needs 281,
  90% needs 531, 95% needs 813 and 99% needs 1,527.

## Coverage after the gap merge

Eleven proposals from the independent gap audit were merged after the producer re-read each
definition in all four tag taxonomy versions: `OtherIntangibleAssetsNet`,
`AccountsAndOtherReceivablesNetCurrent`, `OtherAssetsMiscellaneousNoncurrent`,
`LongTermLoansFromBank`, `LongTermNotesAndLoans`, `LongTermNotesPayable`,
`LoansPayableToBankCurrent`, `NotesPayableToBankCurrent`, `NotesAndLoansPayableCurrent`,
`PensionAndOtherPostretirementAndPostemploymentBenefitPlansLiabilitiesNoncurrent` and
`PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest`. Three borderline
proposals (`InterestAndDebtExpense`, `OtherAccruedLiabilitiesCurrent`,
`OtherAccruedLiabilitiesNoncurrent`) stay unlisted, and `MembersEquity` is not excluded.

An independent review of the merged mapping then changed two things: six single-instrument
borrowing tags (`NotesPayableCurrent`, `ShortTermBankLoansAndNotesPayable`,
`LoansPayableToBankCurrent`, `NotesPayableToBankCurrent`, `LongTermLoansFromBank`,
`LongTermNotesPayable`) became explicit nulls, and `MembersEquity`, `PartnersCapital` and
`MembersCapital` were excluded as ambiguous, because none of their definitions says whether the
noncontrolling share is included. `LoansPayableCurrent` stays mapped.

The draft package was regenerated (444 entries; mapping hash `cd856f9a...`, crosswalk hash
`68d44b3f...`; scratch directory `draft4`, after three stale reason strings were corrected) and `make sec-coverage` rerun into
`var/sec_prepared/coverage-report-4.json` and `unlisted-tags-4.jsonl` in about 35 s:

The later hash-bound companion requirement changes the crosswalk hash and packet rows; these
values are **to be regenerated**. On the eight archives (read-only probe, 3,742,460 rows) the two parent tags give 14,695 resolved
rows, 340 `contradictory_presentation` drops, 46,948 `ambiguous_semantics`, 112 `parenthetical`,
41 `empty_label` and 2,479 `unsupported_statement_code`. The most frequent drops are equal labels
("net loss" 54, "net income" 39) and labels naming the noncontrolling share ("total stockholders
equity excluding noncontrolling interests" 14, which is a genuine parent label dropped by the
over-inclusive word check). Whether the 14,695 kept rows carry any wrong label is not measured.

| Quantity | First report | After the merge and the review |
| --- | --- | --- |
| rows mapped | 772,247 | 771,591 |
| null entries | 102,535 | 107,899 |
| dropped: ambiguous semantics | 61,983 | 62,676 |
| outside-crosswalk rows, balance sheet and income statement | 618,283 | 612,882 |
| outside-crosswalk rows, CI, CF and EQ | 1,408,697 | 1,408,697 |
| distinct unlisted tags, balance sheet | 1,330 | 1,316 |
| distinct unlisted tags, income statement | 1,277 | 1,277 |
| unlisted tags covering 50 / 80 / 90 / 95 / 99% of the unlisted rows | 57 / 281 / 531 / 813 / 1,527 | 55 / 276 / 522 / 803 / 1,517 |

Custom rows (370,216) and the other drop counts are unchanged.

## Coverage with conditional parent-share companions

Measured read-only on 2026-10-19 across the same 3,742,460 prepared rows in 33,942 filings,
using the companion-rule draft described in [sec-crosswalk-data.md](sec-crosswalk-data.md):

| Resolution or drop reason | Before companions | With companions |
| --- | --- | --- |
| mapped by the crosswalk | 771,591 | 786,626 |
| null entry | 107,899 | 107,899 |
| outside crosswalk | 2,021,579 | 2,021,579 |
| custom extension | 370,216 | 370,216 |
| dropped: `ambiguous_semantics` | 62,676 | 47,641 |
| dropped: `parenthetical` | 270,744 | 270,744 |
| dropped: `unsupported_namespace` | 82,489 | 82,489 |
| dropped: `unsupported_statement_code` | 54,161 | 54,161 |
| dropped: `empty_label` | 1,105 | 1,105 |

Resolved rows plus dropped rows still sum to 3,742,460. `NetIncomeLoss` contributes 7,564
new parent-income classifications and `StockholdersEquity` 7,471 new parent-equity
classifications. A companion must be a standard nonparenthetical line in the same
`adsh`/`stmt`/presentation `report`; it is not a reporting-period match. The absent-companion
rows and all three LLC/partnership tags remain `ambiguous_semantics`.
These are draft classification counts, before negative-control override and unknown-role
drops. The linked data runbook gives the per-version drops, role counts, dedupe conflict rates,
changed approval hashes and scratch artifact location. No approvals or datasets were changed.

## Roles

`training/sec_roles.py` and `training/data/sec_crosswalk/role_rules.yaml` implement the rule
table above. The rules file is the one definition: a four-digit SIC from 6000 to 6499 is a
negative control, any other four-digit SIC is corporate, and a missing SIC or one that is not
exactly four ASCII digits is unknown. `sec_fsds.role_for` is not reused because it differs (a
non-numeric SIC is corporate there).

```sh
make sec-roles-draft PREPARED=var/sec_prepared ARCHIVES="var/sec_fsds/2024q3.zip ..." OUT_DIR=<dir>
make sec-roles-approve PREPARED=... ARCHIVES=... ASSESSMENTS=<dir>/role_assessments.json \
    VERDICTS=<file> REVIEWER_ID=... AUTHORITY_ID=... EVIDENCE_REFERENCE=... OUT_DIR=<dir2>
```

`roles-draft` writes one draft `RoleAssessment` per prepared filing (review evidence names the
rule table hash and the filing) and `role_packet.jsonl`: a header with the rule table hash, the
hash of the assessment list, the count and the count per role, then the rule table text. The
reviewer writes a verdict file of the header copied unchanged and one approval of the key
`rule-table:<sha256>`. `roles-approve` re-reads `sub.txt` for the prepared filings, recomputes
every assessment and refuses any difference (a changed role, a missing, extra or repeated
filing), a header that differs from the recomputed one, a rejection, or a verdict on another
table. It then writes the reviewed copies and one approval record per assessment, all citing
the same evidence: the hash of the rule table, the derived assessment list and the verdict file,
so a changed verdict changes the evidence.

## Training examples

`training/sec_examples.py` (`make sec-examples`) turns the prepared payloads into the three
files `make dataset-build` reads. It has not been run on real data: no approvals exist.

```sh
make sec-examples PREPARED=var/sec_prepared OUT_DIR=<new dir> [TRUST_DIR=var/approvals] [REGISTRY=...]
```

- The trust directory (default `var/approvals/`, untracked) holds `reviewed_crosswalk.json`,
  `reviewed_policies.json`, `approvals.json` (the crosswalk and policy records),
  `reviewed_role_assessments.json`, `role_approvals.json` and `authorities.json` (the list of
  allowlisted authority ids, which only the owner writes). A draft artifact, a missing record, an
  unapproved role or an authority outside the list stops the run with the message from
  `resolve_tag`, and nothing is written.
- The run reads each prepared archive through `iter_payloads`, so every call authorizes the
  roster first. Every filing's role is checked against the trust snapshot with one
  `resolve_tag` call; each row is classified (the counted drops above), then resolved with
  `resolve_tag`, once per role and key (or scope, where the crosswalk has no entry), because
  the answer does not depend on the filing. An unresolved key is a counted drop
  `unresolved:<reason>`, never a null.
- A mapped or abstaining row becomes a candidate (a few fields), not an example. Candidates
  are deduplicated per issuer with `dataset_builder.dedupe_group_key` and `dedupe_lowest`, the
  rule the dataset builder uses, in batches of issuers (`--batch-issuers`, default 100); only
  survivors become `NormalizationExample`s. A label that one issuer pairs with two different
  targets is dropped, counted as `conflicting_groups` and `conflicting_rows` (printed on their
  own lines), where the dataset builder would stop. If conflicting rows exceed 5% of the
  candidate rows the run stops and writes nothing, unless `--allow-conflicts-up-to N` names the
  number of conflicting rows to accept. `months` is none, `provenance` is none, `tag` is `namespace:tag` (`custom`
  for a filer tag), `example_id` is `adsh:report:line` (a line number alone repeats across
  reports) and the verification names the approval that resolved the row (crosswalk for a
  mapping or a null entry, policy for a policy abstention).
- Outputs are written as partial files and renamed into place only when the run succeeds
  (a failed rename puts the earlier ones back); never over an existing directory:
  `examples.jsonl`, `sources.jsonl` (the filings' `SourceRecord`s from the index, with their
  archive hashes), `approved.txt` (the content hash of every emitted example) and `counts.json`
  (rows read, drops, rows resolved, rows before and after deduplication, conflicts, examples,
  filings, issuers and peak memory).
- `approved.txt` means "resolved under approved artifacts". It does not say that anyone read
  the example. It is the caller's trust boundary, as for `dataset-build`.

Measured on a synthetic case of 199,998 rows in three filings of two issuers (with the
prepared payloads built beforehand, in a separate process): peak resident memory of
`sec-examples` was 391 MB with every label distinct (133,332 examples; all 199,998 candidates
held in one batch) and 375 MB with 50 distinct labels (100 examples), so holding 200,000
candidates cost about 16 MB over the interpreter's own footprint. Throughput was about 21,000
rows per second in the first case. One role check costs about 12 ms, so the real roster of
33,942 filings needs roughly 7 minutes of checks plus the row work for 3.7 million rows; that
estimate is not a measurement of a real run.

## Monitoring a run

`sec-prepare`, `sec-coverage` and `sec-examples` report stage progress on stderr. A terminal
shows an in-place progress bar; redirected stderr and CI logs show timestamped start, periodic
and finish lines. Existing summaries and generated files remain unchanged.

## Measured: `sec-examples` over eight quarters (2026-10-18)

On the prepared payloads of 2024q3 to 2026q2 (33,942 filings of 5,547 issuers): 3,742,460 rows
read, 3,188,389 rows resolved (696,722 mapped, 2,491,667 abstain), 811,629 examples after
deduplication. Dropped before resolution: 270,744 parenthetical, 82,896 unknown role,
82,489 unsupported namespace, 62,676 ambiguous semantics, 54,161 unsupported statement code,
1,105 empty label. Conflicting-target groups: 4,848 (36,151 rows, 1.1% of rows before
deduplication), dropped and counted. Elapsed 1h53m34s, peak RSS 388,169,728 bytes. This run
had no progress output; the elapsed time is from `time`.
