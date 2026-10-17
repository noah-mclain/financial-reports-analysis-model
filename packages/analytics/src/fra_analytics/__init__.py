"""Pure metrics over mapped statements; document approval belongs to integration."""

from fra_analytics.formatting import format_amount, format_metric
from fra_analytics.frame import Frame, primary_statements, to_frame
from fra_analytics.identities import check_identities
from fra_analytics.metrics.profitability import compute_margins
from fra_analytics.metrics.registry import METRIC_IDS, MetricDescriptor, compute, metric_descriptor
from fra_analytics.policy import Policy, load_policy
from fra_analytics.unit_caveats import UnitNotes, unit_notes

__all__ = [
    "METRIC_IDS",
    "Frame",
    "MetricDescriptor",
    "Policy",
    "UnitNotes",
    "check_identities",
    "compute",
    "compute_margins",
    "format_amount",
    "format_metric",
    "load_policy",
    "metric_descriptor",
    "primary_statements",
    "to_frame",
    "unit_notes",
]
