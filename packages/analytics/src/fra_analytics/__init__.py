"""Pure metrics over mapped statements; document approval belongs to integration."""

from fra_analytics.metrics.profitability import compute_margins
from fra_analytics.policy import Policy, load_policy

__all__ = ["Policy", "compute_margins", "load_policy"]
