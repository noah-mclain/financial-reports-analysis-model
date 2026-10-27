"""Authorize only the fixed direct-template grammar, with independent claim/span checks.

This is deliberately not an arbitrary prose or numeric normalization checker.
"""

from fra_analytics import format_metric, metric_descriptor
from fra_core.citations import place_claims
from fra_core.presentation import caveat_text
from fra_core.schemas import Footnote, Narrative
from fra_core.schemas.narrative import GroundingResult
from fra_model.payload import SUMMARY_IDS, SummaryPayload
from fra_model.templates import PARTIAL_TEMPLATE_VERSION, TEMPLATE_VERSION, templates


class GroundingError(ValueError):
    pass


def check_template(payload: SummaryPayload, narrative: Narrative) -> Narrative:
    try:
        candidate = Narrative.model_validate(narrative.model_dump())
        if (
            candidate.origin != "direct_template"
            or candidate.template_version
            != (PARTIAL_TEMPLATE_VERSION if payload.partial else TEMPLATE_VERSION)
            or candidate.language != payload.language
            or not payload.facts
        ):
            raise ValueError("unsupported template metadata or empty payload")
        order = [SUMMARY_IDS.index(f.metric.metric_id) for f in payload.facts]
        if order != sorted(set(order)):
            raise ValueError("payload facts duplicated or out of order")
        if payload.partial:
            included = {f.metric.metric_id for f in payload.facts}
            omitted = [o.metric_id for o in payload.omissions]
            if payload.language != "en" or omitted != [m for m in SUMMARY_IDS if m not in included]:
                raise ValueError("partial payload must explain every omitted metric in order")
        elif payload.omissions or payload.scanned:
            raise ValueError("partial notices on a complete payload")
        refs = {(f.metric.metric_id, f.period.key): f for f in payload.facts}
        sentences = [
            s
            for section in candidate.sections
            for s in section.sentences
            if not payload.partial or s.claims
        ]
        if len(sentences) != len(refs):
            raise ValueError("missing or extra sentences")
        used = set()
        for sentence in sentences:
            if len(sentence.claims) != 1:
                raise ValueError("exactly one complete value claim required")
            claim = sentence.claims[0]
            key = (claim.metric_id, claim.period_key)
            if key not in refs or key in used:
                raise ValueError("missing or duplicate metric-period reference")
            used.add(key)
            fact = refs[key]
            descriptor = metric_descriptor(claim.metric_id)
            if (
                fact.metric.period_key != claim.period_key
                or descriptor is None
                or descriptor.unit != fact.metric.unit
            ):
                raise ValueError("fact identity/unit mismatch")
            value = format_metric(fact.metric, payload.language)
            if value is None or claim.text_span != value:
                raise ValueError("claim does not state the authorized formatted value")
            placed = place_claims(sentence)
            if len(placed) != 1 or sentence.text.count(value) != 1:
                raise ValueError("ambiguous value occurrence")
            # Source caveats come from the metric itself, so a payload whose footnotes were
            # dropped, or whose id/caveat/currency/text were changed, cannot authorize a
            # template that silently omits or misstates them.
            currencies = {r.currency for cells in fact.metric.inputs.values() for r in cells}
            if not currencies:
                raise ValueError("metric has no source inputs")
            if len(currencies) != 1:
                raise ValueError("metric currency mismatch")
            currency = next(iter(currencies))
            required_ids = [f"{c}-{currency}" for c in sorted(fact.metric.caveats)]
            required = [
                Footnote(
                    id=footnote_id,
                    caveat_id=caveat_id,
                    currency=currency,
                    text=caveat_text(caveat_id, currency, payload.language),
                )
                for footnote_id, caveat_id in zip(
                    required_ids, sorted(fact.metric.caveats), strict=True
                )
            ]
            if list(fact.footnotes) != required:
                raise ValueError("payload footnotes differ from the metric's source caveats")
            if sentence.footnotes != required_ids:
                raise ValueError("claim caveat references mismatch")
        # Dates, durations, punctuation, units, vocabulary, sections and all remaining text
        # must be exactly the authorized grammar. No general year/numeric exemptions exist.
        expected = templates(payload)
        if candidate.sections != expected.sections or candidate.footnotes != expected.footnotes:
            raise ValueError("text or caveats outside authorized template grammar")
        return candidate.model_copy(update={"grounding": GroundingResult.PASSED}, deep=True)
    except (ValueError, KeyError) as error:
        raise GroundingError(str(error)) from error
