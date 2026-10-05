"""Pure metrics over mapped statements; document approval belongs to integration."""

from fra_analytics.frame import Frame, primary_statements, to_frame
from fra_analytics.identities import check_identities
from fra_analytics.metrics.profitability import compute_margins
from fra_analytics.metrics.registry import METRIC_IDS, compute
from fra_analytics.policy import Policy, load_policy

__all__ = [
    "METRIC_IDS",
    "Frame",
    "Policy",
    "check_identities",
    "compute",
    "compute_margins",
    "load_policy",
    "primary_statements",
    "to_frame",
]
