# SEC crosswalk data package

This is the reviewable crosswalk that [the crosswalk contract](sec-crosswalk-contract.md)
consumes, built from vocabulary only: the tag definitions in `tag.txt` of the Financial
Statement Data Sets archives. No filer row (`pre.txt`, `num.txt`, `sub.txt`) is read, and the
code refuses any archive member other than `tag.txt`. Nothing here is approved.

## Pieces

| Piece | File |
| --- | --- |
| Curated mapping, tracked data | `training/data/sec_crosswalk/us_gaap_mapping.yaml` |
| Vocabulary reader, draft builder, policies | `training/sec_crosswalk_draft.py` |
| Review packet, owner sample, approval records | `training/sec_crosswalk_review.py` |
| Commands | `training/sec_crosswalk_cli.py` |

`training/data/` is ignored except `sec_crosswalk/`; the downloaded archives stay out of Git.
Approval records go to the owner's local trust store `var/approvals/`, which is ignored.

## Interpretations

- **Version.** A vocabulary `version` such as `us-gaap/2024` is `<namespace>/<tag taxonomy
  version>`: the namespace is the text before the slash, the tag taxonomy version the four
  digits after it. Only tags with `custom` = 0 and `abstract` = 0 are read, and a standard tag
  whose version does not match that form is an error naming the member and line.
- **Custom tags** are not in this vocabulary. They share one scope, namespace `custom` and
  tag taxonomy version `extension`. The decoder that reads filer rows must map `custom` = 1 to
  it.
- **Statement codes** (`STATEMENT_CODES` in `sec_crosswalk_draft.py`): BS is the balance sheet,
  IS income, CI comprehensive income, CF cash flow, EQ equity.
- **Counted drop reasons of the filer-row decoder** (task B). A row it cannot send to
  resolution is dropped with a counted reason and never turned into a null target:
  - `unsupported_statement_code`: any other statement code (UN, SI, CP and so on) has no
    `StatementType`.
  - `unsupported_namespace`: a standard tag outside the mapping's namespace (for example `ifrs`
    or `us-gaap-ebp`), for which there is no mapping or policy.
  - `ambiguous_semantics`: a tag listed under `excluded:` in the mapping.
- **Mapping rows** exist for BS and IS only, because the taxonomy has items for those two
  statements. CI, CF and EQ lines are covered by an outside-crosswalk policy.
- **Definitions change between versions.** A tag's `doc` can be reworded from one tag taxonomy
  version to the next, so each mapping row lists one or more `doc_phrases` quoted from the
  definition. The builder refuses a tag whose definition in any supplied version holds none of
  them, so a reworded definition is looked at again before it is used. `tlabel` in `tag.txt`
  can be a filer's own label; the `doc` is the definition that was read.

## What the draft holds

One entry per (namespace, version, statement, tag) for each version present in the supplied
vocabularies. A mapped tag missing from a version's vocabulary gets no entry for that version.
`semantic_reference` is `tag.txt <version> <tag>` and `semantic_evidence_sha256` hashes that
tag's `tlabel` and `doc` in that version. The review block is `draft`, producer
`crosswalk-draft-1`, with the mapping file's SHA-256.

Five drafted policies cover every standard version and the `custom` / `extension` scope:
outside-crosswalk for BS and IS, outside-crosswalk for CI, CF and EQ, outside-crosswalk for
custom tags, and negative-control for standard and for custom tags. Namespaces other than the
mapping's (for example `ifrs`) have no policy, so they stay unresolved.

The mapping maps only tags that mean exactly the item. Near-synonyms and tags that cover two
items are explicit nulls with a reason.

Five tags are `excluded` instead. `NetIncomeLoss` and `StockholdersEquity` are each defined as
the parent's share, while filers without a noncontrolling interest print it as the plain total,
so the tag alone cannot choose between `net_income` and `net_income_attributable_parent`, or
between `total_equity` and `equity_attributable_parent`. An excluded tag is neither mapped,
null nor covered by an abstention policy; the decoder drops its rows (`ambiguous_semantics`)
before resolution, and `load_mapping` exposes them typed as `CuratedMapping.excluded`. The
other three, `MembersEquity`, `PartnersCapital` and `MembersCapital`, are limited liability
company and partnership equity tags whose definitions do not say whether the noncontrolling
share is included (`MembersEquity` says the parent's share for 2023 to 2025 and plain members'
equity for 2026). A tag cannot be both a row and excluded. The search for other tags whose item depends on whether a
noncontrolling interest is shown (the continuing-operations and comprehensive-income
analogues) found none to add: they either name a narrower quantity than any item, so they are
null rows, or sit on statements the taxonomy does not cover.

The cost: `net_income` and `total_equity` supervision comes only from the unambiguous tags
(`ProfitLoss` and `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`).
That is a coverage bias toward filers that show a noncontrolling interest, not a label error.
It is to be measured in task B and revisited.

Three items have no tag (`net_income_attributable_parent`, `equity_attributable_parent`,
`other_reserves`) and are listed under `unmapped_items` with the reason.

## Counts

Measured with the eight archives `2024q3` to `2026q2` in `var/sec_fsds/`:

```sh
make sec-crosswalk-draft ARCHIVES="var/sec_fsds/2024q3.zip ... var/sec_fsds/2026q2.zip" OUT_DIR=/tmp/draft
```

- 444 entries over 112 distinct (statement, tag) pairs: 279 mapped and 165 null. The pairs
  appear under 2023 (111 entries), 2024 (112), 2025 (111) and 2026 (110) tag taxonomy versions.
  Five tags are excluded (`NetIncomeLoss`, `StockholdersEquity`, `MembersEquity`,
  `PartnersCapital`, `MembersCapital`).
- 70 mapped (statement, tag) pairs reach 44 of the 47 items. The mapping file has 112 rows: 70
  mapped and 42 null.
- Plan-critical items: `revenue` has two tags (`Revenues` and
  `RevenueFromContractWithCustomerExcludingAssessedTax`; the Including variant is null because
  it is gross of collected sales tax). `net_income` has `ProfitLoss`, `total_assets` has
  `Assets`, `total_equity` has two (`StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`
  and `PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest`),
  `total_current_assets` has `AssetsCurrent` and `total_current_liabilities` has
  `LiabilitiesCurrent`.
- Eleven tags were added after the first coverage audit (see
  [sec-source-preparation.md](sec-source-preparation.md)). Three audit proposals were left
  unlisted as borderline (`InterestAndDebtExpense`, `OtherAccruedLiabilitiesCurrent`,
  `OtherAccruedLiabilitiesNoncurrent`). An independent review then set six single-instrument
  borrowing tags to explicit nulls (`NotesPayableCurrent`, `ShortTermBankLoansAndNotesPayable`,
  `LoansPayableToBankCurrent`, `NotesPayableToBankCurrent`, `LongTermLoansFromBank`,
  `LongTermNotesPayable`: "single borrowing instrument; narrower than the item") and excluded
  `MembersEquity`, `PartnersCapital` and `MembersCapital`, whose definitions do not say whether
  the noncontrolling share is included.

## Alias rule and tags that share an item

A tag is mapped when the item's aliases list the concept without a qualifier, and null when the
tag is narrower than, broader than or ambiguous between items. Several tags may therefore land
on one item. These do (read from the mapping file):

| Item | Tags |
| --- | --- |
| `revenue` | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax` |
| `cost_of_revenue` | `CostOfRevenue`, `CostOfGoodsAndServicesSold` |
| `selling_general_admin` | `SellingGeneralAndAdministrativeExpense`, `GeneralAndAdministrativeExpense`, `SellingExpense` |
| `depreciation_amortization` | `Depreciation`, `DepreciationAndAmortization` |
| `finance_costs` | `InterestExpense`, `InterestExpenseNonoperating` |
| `trade_receivables` | `AccountsReceivableNetCurrent`, `AccountsReceivableNet`, `ReceivablesNetCurrent`, `AccountsAndOtherReceivablesNetCurrent` |
| `other_current_assets` | `OtherAssetsCurrent`, `PrepaidExpenseAndOtherAssetsCurrent`, `PrepaidExpenseCurrent` |
| `right_of_use_assets` | `OperatingLeaseRightOfUseAsset`, `FinanceLeaseRightOfUseAsset` |
| `intangible_assets` | `IntangibleAssetsNetExcludingGoodwill`, `OtherIntangibleAssetsNet` |
| `other_non_current_assets` | `OtherAssetsNoncurrent`, `OtherAssetsMiscellaneousNoncurrent` |
| `trade_payables` | `AccountsPayableCurrent`, `AccountsPayableTradeCurrent` |
| `short_term_borrowings` | `ShortTermBorrowings`, `BankOverdrafts` |
| `current_portion_long_term_debt` | `LongTermDebtCurrent`, `LoansPayableCurrent`, `NotesAndLoansPayableCurrent` |
| `lease_liabilities_current` | `OperatingLeaseLiabilityCurrent`, `FinanceLeaseLiabilityCurrent` |
| `other_current_liabilities` | `OtherLiabilitiesCurrent`, `AccruedLiabilitiesCurrent` |
| `long_term_borrowings` | `LongTermDebtNoncurrent`, `LongTermLoansPayable`, `LongTermNotesAndLoans` |
| `lease_liabilities_non_current` | `OperatingLeaseLiabilityNoncurrent`, `FinanceLeaseLiabilityNoncurrent` |
| `other_non_current_liabilities` | `OtherLiabilitiesNoncurrent`, `PensionAndOtherPostretirementDefinedBenefitPlansLiabilitiesNoncurrent`, `PensionAndOtherPostretirementAndPostemploymentBenefitPlansLiabilitiesNoncurrent` |
| `total_equity` | `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`, `PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest` |

`LoansPayableCurrent` ("loans" is a listed alias) and the combined `NotesAndLoansPayableCurrent`
map to the current portion of long-term debt; the notes-only and bank-only siblings are
explicit nulls because each is a single borrowing instrument. `OperatingLeaseLiability` (no current
split) and `InvestmentIncomeInterestAndDividend` (includes dividends) are explicit nulls.

Tags the vocabulary does not hold are not listed. Coverage of unlisted tags is audited from the
real tag frequencies (`make sec-coverage`) before approval.

## Review procedure

1. `make sec-crosswalk-draft` writes `crosswalk.json`, `policies.json` and
   `review_packet.jsonl` into a new directory and prints the mapping, crosswalk and policy
   hashes. It fails, writing nothing, if any mapping row or excluded tag matches no tag in the
   supplied archives. The packet's first line is the subjects header
   `{"kind": "subjects", "mapping_sha256", "crosswalk_sha256", "policies": {id: sha256}}`.
   It names the exact artifacts the review covers. After it come one row per entry (tag,
   version, statement, `tlabel`, `doc`, target id with its aliases, statement and natural sign,
   and the reason), one per policy and one per excluded tag (with its definition in each
   version).
2. A reviewer other than the producer reads the whole packet and writes a verdict file: JSONL
   whose first line is the packet's subjects header copied unchanged, then one
   `{"key", "verdict": "approve" | "reject", "note"}` per entry key
   (`us-gaap|2024|balance|Assets`), policy key (`policy:<id>`) and excluded key
   (`excluded:IS:NetIncomeLoss`).
3. The owner checks a sample: `sec_crosswalk_cli.py sample` returns the header, every entry for
   a plan-critical item, every policy and excluded tag, and a seeded draw of the rest that
   includes null rows. Critical entries, policies and excluded tags are always kept, so a small
   `--n` still returns them.
4. `make sec-crosswalk-approve` first checks the artifacts it is given: the crosswalk and every
   policy must be a draft from producer `crosswalk-draft-1`, drafted from the mapping file given
   (`--mapping`, default the committed file; its hash is each artifact's review hash), and the
   header recomputed from them must equal the verdict file's first line, so a verdict cannot be
   reused after an entry or policy is edited. It then refuses unless every entry, policy and
   excluded tag has an explicit approve and none is rejected. It writes the reviewed copies of
   the crosswalk and policies (status `reviewed`, because a draft cannot be used and the status
   is part of the hash an approval binds) and `approvals.json` with one record per artifact.
   The evidence hash is the SHA-256 of the verdict file. The reviewer must differ from the
   producer. All output files are created exclusively and none is overwritten.

The reviewer is a separate agent and the owner checks a sample. A reviewer from the same model
family can share this draft's errors, so the owner's sample is the independent check, not a
formality. The approval records are an input to the trust snapshot the caller builds; the
caller still authenticates reviewers and authorities.

## The trust directory

`make sec-examples` reads the reviewed artifacts and their approvals from one directory the
owner controls (`var/approvals/` by default, untracked). Nothing in the repository writes there:
the owner copies the files in after checking them. All files are JSON.

| File | Produced by | Shape |
| --- | --- | --- |
| `reviewed_crosswalk.json` | `make sec-crosswalk-approve` | one `Crosswalk` object, `review.status` `reviewed` |
| `reviewed_policies.json` | `make sec-crosswalk-approve` | array of `AbstentionPolicy` objects, each `reviewed` |
| `approvals.json` | `make sec-crosswalk-approve` | array of `ApprovalRecord` objects: one for the crosswalk, one per policy |
| `reviewed_role_assessments.json` | `make sec-roles-approve` | array of `RoleAssessment` objects, one per prepared filing, each `reviewed` |
| `role_approvals.json` | `make sec-roles-approve` | array of `ApprovalRecord` objects, one per role assessment |
| `authorities.json` | the owner, by hand | array of strings: the authority ids the owner accepts |

An `ApprovalRecord` has `subject_kind` (`crosswalk`, `abstention_policy` or `role_assessment`),
`subject_sha256`, `producer_id`, `reviewer_id`, `authority_id`, `status` (`approved`),
`evidence_reference` and `evidence_sha256`. The adapter accepts a record only if its
`authority_id` is in `authorities.json`, its producer matches the artifact's, its status is
`approved` and its `subject_sha256` equals the artifact's content hash. A draft artifact, a
missing record, an unlisted authority or a role without a record stops the run.

`authorities.json` is the owner's own statement of whose approvals count, so only the owner
writes it. An example, using the authority id that the review commands take as `AUTHORITY_ID`
(a proposal; any string works if the two agree):

```json
["owner-sample-check-2026-10"]
```

The reviewer agent's own id goes to `REVIEWER_ID` and must differ from the draft producers
(`crosswalk-draft-1` and `role-rules-1`).

## Not done

- Nothing is approved, and no approval record exists.
- No `pre.txt`, `num.txt` or `sub.txt` was read; the decoder for filer rows is separate work.
- No role assessment (corporate or negative control) is produced.
- The taxonomy is unchanged; what it cannot represent is null with a reason.
- Net income and equity lines of filers without a noncontrolling interest stay unsupervised
  until the two excluded tags are revisited.
