# 13. Unit caveats: from extraction to the written summary

Status: v1, 2026-10-01. A plan, not yet built. It follows the owner's decision D11
([06-decisions-and-risks.md](06-decisions-and-risks.md)): a statement that prints no unit
multiplier is not held for review; its figures are used, and every use that depends on the
multiplier carries a footnote. This document says how that footnote travels from the structure
stage ([11](11-ingest-structure.md), [12](12-ingest-review.md)) through analytics and narration
to the page, and what the language model is told and held to.

## The problem

Structure reads scale from the header, the caption and the page text. When none of them names a
multiplier, it records scale 1 and the flag `scale_missing`. Three other fallbacks work the same
way for currency: `currency_from_domicile`, `currency_inferred`, and (held, not covered here)
`currency_missing`.

An unstated multiplier is normal, not a fault. Egyptian filings print whole pounds and say only
"EGP" or "جنيه مصري". But the tool cannot tell "printed in units" from "printed in thousands and
the word was lost", and if it guesses wrong every amount is off by 1,000 (R1). So the figures
are used, the assumption is stated wherever it matters, and nothing downstream may present it
as a fact.

## Measured before designing

From the golden output, structure version 4.

| Question | Measured | Consequence |
|----------|----------|-------------|
| How common is an unstated multiplier? | 10 of 12 documents, every Juhayna and Edita statement, carry `scale_missing`. Almarai states thousands | It is the usual case for this market. Holding on it would hold everything |
| What do the pages say? | On the six pages compared with their images (Edita IFRS 8 to 10, Edita 2024 AR 5 to 7), the header names the currency (`EGP`, `جنيه مصري`) and no multiplier | "Currency named, multiplier absent" is a recognisable state of its own, different from "nothing found" |
| Do magnitudes support the assumption? | Median printed figure on the first statements with no multiplier: 3.4 x 10^7 to 1.9 x 10^9, leaving out three statements whose cells are merged or mostly lost. On Almarai, printed in thousands: 2.2 x 10^5 to 1.4 x 10^6 | Large printed figures are evidence for units, recorded beside the assumption. They cannot prove it: a small company in units and a large one in thousands overlap |
| Which outputs depend on the multiplier? | Ratios, times and days are scale-invariant (T2 tests this). Only amounts in currency, and growth between two documents with different assumptions, depend on it | The footnote belongs on currency amounts, not on margins or ratios. Most of a summary needs none |

## Design

One object, a caveat, created once in structure and carried unchanged. Each later stage only
decides which of its outputs the caveat reaches.

```python
class Caveat(BaseModel):            # fra_core.schemas.caveat, frozen
    id: Literal[
        "scale_assumed_units",      # no multiplier printed; figures taken as single units
        "currency_from_domicile",   # currency from the country of incorporation, not the statement
        "currency_inferred",        # currency from the rest of the document
    ]
    scope: Literal["currency_amounts"]
    evidence: dict[str, str]        # what the assumption rests on, for the review report
```

### 1. Structure (ingest)

- `detect_metadata` keeps `scale_missing` as the flag and also returns the caveat. Evidence for
  `scale_assumed_units`: whether a currency was named in the header (`currency_in_header`), and
  the median printed figure of the statement (`median_figure`).
- `Statement` gains `caveats: list[Caveat]`. A statement whose scale was read has none for scale.
- Two rules beside it:
  - A document's statements must agree. When one statement states a multiplier and another of
    the same document states none, the stated one is used for both and the second carries
    `scale_from_sibling`, not the caveat. When two state different multipliers, that is
    `scale_conflict`, which already holds.
  - A statement with the caveat whose median printed figure is under 10^4 carries
    `scale_implausible` and is held: figures that small in single units mean a lost multiplier
    or a broken read. (The threshold is checked against the corpus `dev` pool before it is fixed.)
- The review report prints the caveat and its evidence in the statement's line, and
  `StatementReview.warnings` keeps listing it.
- The manifest's `scale: unconfirmed` stays until the owner confirms each document; the eval
  scores scale only where it is confirmed, as now.

### 2. Analytics

- `to_frame` carries each statement's caveat ids on its rows.
- `MetricValue` gains `caveats: list[str]`. A metric takes a caveat when its unit is `currency`
  or `per_share` and any input row carries it. Ratios, times and days take none.
- Growth and CAGR between periods of one statement take none: both periods share the
  assumption, so it cancels. Across two documents they take it when the documents differ.
- Charts: an axis in currency whose series carries the caveat labels the unit as assumed
  ("EGP, as printed") and the chart JSON carries the caveat id (T6 asserts both).

### 3. Narration: what the model is told, and what it is held to

The model never produces a number (N1), so it cannot get the amount wrong. What it can get
wrong is the wording around it, so the handling has three layers, and only the first relies on
the model.

1. **Told.** The payload lists the document's caveats with a one-line meaning each, and marks
   every metric that carries one. The prompt says: an amount marked with a caveat is "as
   printed"; do not restate it in other units, do not compare its size with another company or
   an outside figure, and do not call it large or small in absolute terms. Ratios and changes
   are unaffected and should carry the analysis.
2. **Attached, not asked for.** `Sentence` gains `footnotes: list[str]` and `Narrative` gains
   `footnotes: list[Footnote(id, text)]`. After generation, code adds the caveat id to every
   sentence that has a claim on a caveated metric, and adds the footnote text from a fixed
   template in the narrative's language. The model does not write footnote text and cannot
   omit a footnote.
3. **Checked.** The grounding checker gains two rules. A claim on a caveated metric whose
   sentence lacks the footnote fails (this guards the code in layer 2, and the template
   fallback, which must attach it too). A sentence with a caveated amount that also contains a
   multiplier word the formatter did not produce (thousand, million, billion, ألف, مليون,
   مليار) fails and is regenerated once, then replaced by the template sentence.

Footnote text, fixed, one per caveat and language:

| Caveat | English | Arabic |
|--------|---------|--------|
| `scale_assumed_units` | Amounts are shown as printed. The statement names no unit multiplier such as thousands or millions, so they are taken to be in single {currency}. | المبالغ معروضة كما وردت. لم تذكر القائمة مضاعفاً للوحدة مثل الآلاف أو الملايين، ولذلك اعتُبرت بالـ{currency} دون مضاعف. |
| `currency_from_domicile` | The statement does not name its currency. {currency} is taken from the company's country of incorporation. | لم تذكر القائمة عملتها. اعتُمد {currency} بحسب بلد تأسيس الشركة. |
| `currency_inferred` | The statement does not name its currency. {currency} is the currency named most often in the document. | لم تذكر القائمة عملتها. اعتُمد {currency} لأنه العملة الأكثر ذكراً في المستند. |

### 4. Display

- Statement view: the caveat sits in the statement header, beside currency and scale, with its
  evidence on hover.
- Every displayed currency amount from a caveated statement carries the footnote mark; ratios
  do not.
- Summary: footnote marks on the sentences, the footnote texts once below the summary.
- A held statement is still not narrated (N4). The caveat never turns a hold into a pass or a
  pass into a hold, except through `scale_implausible`.

## Where it lands

| Stage | Work | When |
|-------|------|------|
| Structure | `Caveat`, `Statement.caveats`, sibling rule, `scale_implausible`, review report line, tests | With part 3b, on `ingest-structure`, once this plan is agreed |
| Analytics | Caveats on frame rows and `MetricValue`, chart labels | Week 2, with the metric registry |
| Narration | Payload, prompt rule, `Sentence.footnotes`, `Narrative.footnotes`, attach step, two grounding rules, templates in both languages | Week 3, with the grounding checker |
| Display | Header, footnote marks, summary footnotes | Week 4, with the results page |

## Testing

| Area | Cases |
|------|-------|
| Structure | No multiplier and a currency in the header gives scale 1, `scale_missing` and the caveat with its evidence; a stated multiplier gives no caveat; a sibling's stated multiplier is taken with `scale_from_sibling`; median under the threshold gives `scale_implausible` and a hold; the caveat alone does not hold |
| Analytics | A currency metric from a caveated statement carries the caveat; a ratio from the same rows does not; growth within one statement does not; scaling every input by 1,000 changes caveated amounts and no ratio (extends T2) |
| Narration | A sentence with a claim on a caveated amount gets the footnote; a sentence with only ratio claims gets none; the template fallback attaches it; a sentence that restates a caveated amount with an unformatted "million" fails; footnote text comes from the template in the narrative's language (extends T8) |
| End to end | Edita IFRS: the summary's amounts carry one footnote, its margins none. Almarai: no scale footnote, one currency footnote (`currency_from_domicile`) |

## Risks

| ID | Risk | Mitigation |
|----|------|------------|
| R43 | The assumption is wrong and every amount is off by 1,000, with a footnote the reader skips | Ratios carry the analysis and do not depend on it; `scale_implausible` holds the clear cases; the footnote is on every amount, not once in small print; the manifest confirms scale on documents we score |
| R44 | Footnotes on everything, so they stop being read | Scope is currency amounts only; one text per caveat per document, repeated marks |
| R45 | The model words around the caveat ("roughly EGP 15 billion") | It cannot produce a number at all (N1); multiplier words beside a caveated amount fail grounding; the template is the fallback |
| R46 | A later stage drops the caveat | One object from structure to display; the grounding rule fails any caveated claim without its footnote, including in the template path |
