"""The reader every metric formula takes its figures through.

A formula never touches the frame. It asks for an item and gets a ``Decimal`` in currency units
or ``None``, and the reader records what it was given (the cells, for provenance) and why it
gave nothing (the flags). That is how a metric with a missing input comes out null with a flag
naming it, and how every value comes out with the cells it rests on.

D1 (zero and negative denominators, through ``division.divide``), D2 (averages with no opening
balance) and D4 (days) are applied here, once, so no formula can forget them. Margins are not
read here: they come from ``profitability.compute_margins``, which owns their D1 setting.

Signs. A line item whose taxonomy ``natural_sign`` is minus (an expense) is read as a
magnitude: issuers print costs in brackets or without them, and a ratio must not depend on
which. The sign therefore never changes a result and nothing is flagged for it. An item with a
plus sign is read as printed, since a negative profit or a negative equity is data.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fra_analytics.frame import Frame, FrameRow
from fra_analytics.metrics.division import arithmetic_context, divide
from fra_analytics.period_math import months_before
from fra_analytics.policy import Policy
from fra_analytics.unit_caveats import UNIT_FLAGS
from fra_core.schemas import Period
from fra_core.taxonomy.loader import load_taxonomy


class Inputs:
    """The figures one metric uses for one period, and what was said about them."""

    def __init__(self, frame: Frame, policy: Policy, period: Period) -> None:
        self._frame = frame
        self._policy = policy
        self.period = period
        self.cells: dict[str, list[FrameRow]] = {}
        self.flags: list[str] = []

    def flag(self, name: str) -> None:
        if name not in self.flags:
            self.flags.append(name)

    def _cite(self, role: str, rows: list[FrameRow]) -> None:
        """Record the cells a role rests on, each once however many formulas read it."""
        cited = self.cells.setdefault(role, [])
        cited.extend(r for r in rows if r not in cited)
        for row in rows:
            for flag in self._frame.statement(row.statement_id).flags:
                # Unit assumptions and contradictions are handled by unit_notes, once.
                if flag not in (
                    *UNIT_FLAGS,
                    "scale_missing",
                    "currency_from_domicile",
                    "currency_inferred",
                ):
                    self.flag(flag)
            for flag in row.flags:
                self.flag(flag)

    # Reading -------------------------------------------------------------------------------

    @arithmetic_context()
    def _read(
        self, item: str, *, end_date: date, months: int | None, role: str | None = None
    ) -> Decimal | None:
        canonical = load_taxonomy().by_id(item)
        if canonical is None:
            msg = f"metric reads {item!r}, which is not a canonical item"
            raise ValueError(msg)
        role = role or item
        rows = self._frame.lookup(item, canonical.statement, end_date=end_date, months=months)
        if len(rows) > 1:
            self.flag(f"duplicate_input:{role}")
            self._cite(role, rows)
            return None
        if not rows:
            self.flag(f"missing_input:{role}")
            if self._frame.unmapped_in(canonical.statement):
                # The item is absent, and the statement holds rows mapping could not place:
                # the figure may be among them. Nothing says it is.
                self.flag(f"input_may_be_unmapped:{role}")
            return None
        [row] = rows
        self._cite(role, [row])
        # An expense is a magnitude whichever sign it is printed with (module docstring).
        return abs(row.value) if canonical.natural_sign == "-" else row.value

    def flow(self, item: str) -> Decimal | None:
        """The item over this period."""
        return self._read(item, end_date=self.period.end_date, months=self.period.months)

    def closing(self, item: str) -> Decimal | None:
        """The balance at the end of this period."""
        return self._read(item, end_date=self.period.end_date, months=None)

    @arithmetic_context()
    def average(self, item: str) -> Decimal | None:
        """D2: the mean of the opening and closing balance. With no opening balance, the closing
        balance and the flag ``average_fallback_to_closing``. With no closing balance, nothing."""
        closing = self.closing(item)
        if closing is None:
            return None
        months = self._months()
        if months is None:
            return None
        opening_date = months_before(self.period.end_date, months)
        canonical = load_taxonomy().by_id(item)
        assert canonical is not None  # closing() has checked
        opening_rows = self._frame.lookup(
            item, canonical.statement, end_date=opening_date, months=None
        )
        if len(opening_rows) > 1:
            self.flag(f"duplicate_input:{item}@opening")
            self._cite(item, opening_rows)
            return None
        if not opening_rows:
            self.flag("average_fallback_to_closing")
            return closing
        [opening] = opening_rows
        self._cite(item, [opening])
        self.cells[item].remove(opening)
        self.cells[item].insert(0, opening)
        return (opening.value + closing) / 2

    def _months(self) -> int | None:
        """The length of the period, or None with the flag ``period_not_a_duration`` for an
        instant: the schema allows one on any statement, and a flow over it has no length."""
        if self.period.months is None:
            self.flag("period_not_a_duration")
        return self.period.months

    def prior_flow(self, item: str) -> Decimal | None:
        """The item over the same length of time ending a year earlier."""
        return self._read(
            item,
            end_date=months_before(self.period.end_date, 12),
            months=self.period.months,
            role=f"{item}@prior_period",
        )

    # Arithmetic ----------------------------------------------------------------------------

    @staticmethod
    @arithmetic_context()
    def total(*values: Decimal | None) -> Decimal | None:
        """The sum, or None when any part is missing: a missing part is never a zero."""
        if any(v is None for v in values):
            return None
        return sum((v for v in values if v is not None), Decimal(0))

    def ratio(self, numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
        """D1 through ``divide``: null when a part is missing, and null with the reason flagged
        for a zero or negative denominator."""
        flags: list[str] = []
        value = divide(numerator, denominator, negative="null", flags=flags)
        for name in flags:
            self.flag(name)
        return value

    @arithmetic_context()
    def growth(self, current: Decimal | None, prior: Decimal | None) -> Decimal | None:
        """The change from ``prior`` to ``current`` as a fraction. A growth rate from a zero or a
        negative base is undefined: null with the flag ``divide`` gives, whatever the current
        figure."""
        ratio = self.ratio(current, prior)
        return None if ratio is None else ratio - 1

    def days(self) -> Decimal | None:
        """D4: the policy's day count for a year, the actual days for any other period. None
        with ``period_not_a_duration`` for an instant."""
        months = self._months()
        if months is None:
            return None
        if months == 12:
            return Decimal(self._policy.day_count_basis)
        return Decimal((self.period.end_date - months_before(self.period.end_date, months)).days)

    @property
    def include_leases(self) -> bool:
        return self._policy.include_lease_liabilities
