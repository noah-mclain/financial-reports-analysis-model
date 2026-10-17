# Direct-template summaries

`fra_model.build_summary(analysis)` returns `SummaryBuilt` with a checked narrative, or
`SummarySuppressed` with explicit reasons. It performs no inference, training, downloads or
financial calculations. The existing worker persists that result; the API rechecks direct
templates before displaying them. Expected suppression leaves the analysis review status alone.
Internal/checker errors fail the existing job without retry.

The initial grammar covers current ratio, total debt, net debt to EBITDA, net margin, EBITDA
and DSO, in that order and at most one sentence per metric. It selects the newest period of
each metric's anchoring statement before filtering facts. A missing, undefined, flagged or
untraceable newest fact never falls back to an older value. Instant dates and duration months
are explicit; there are no comparisons, direction words, qualitative judgments or annualization.
Unsupported languages, duplicate identities, inconsistent reviews and unresolved notices suppress
the summary. Unsupported individual facts are omitted; no eligible facts produces suppression.

`fra_analytics.formatting` is the sole numeric formatting authority. It converts a stored finite
float once with `Decimal(str(value))`, using a private precision-34, half-even context. It does
not recover precision lost upstream or recalculate a metric from its inputs. Decimal ties are
intentional: 2.675 times displays as 2.68× (binary float formatting displayed 2.67×). Amounts
are never converted to compact multipliers. Per-share values preserve all stored Decimal digits.
Arabic uses ASCII digits and localized suffixes. Unit/currency footnotes use the fixed core
language templates and analytics' existing applicability rules, including cross-statement ratios.

`check_template(payload, candidate)` accepts only versioned direct templates. It validates the
complete grammar, section order, exact claim references and formatted spans, date/month text,
unique placement and caveat references/text. It rejects additional prose or altered numeric
forms, including Arabic-digit substitutions. An unchecked narrative becomes passed only after
this check; direct templates never claim that model generation failed. Historical narrative
origins default to unknown and grounding defaults to unchecked. Legacy summaries remain visibly
identified as stored text, without verified-template or model-generation attribution.

This slice does **not** implement arbitrary generated-prose grounding, numeric normalization,
comparative polarity, model repair/retry/fallback or a runtime client. It does not complete T8,
V9's independent numerical/manual audit, G-N1, Gate D, D20, Week 3 or an Arabic semantic audit.
Synthetic regression coverage is evidence for this restricted grammar only. Model-based narration
and the independent audits require separate work and the prescribed evaluation gates.
