"""The shape every report shares, so one renderer serves screen, CSV, XLSX and PDF.

Money cells hold EXACT paise (int or Fraction). Rounding happens once, when a cell is rendered
(decision D-28e); a total is summed exactly and rounded once, so it can differ from the sum of the
displayed lines by Rs 0.01 - the report says so."""

from dataclasses import dataclass, field
from fractions import Fraction

TEXT, INT, DATE, DATETIME, MONEY, PERCENT, NUMBER = (
    "text",
    "int",
    "date",
    "datetime",
    "money",
    "percent",
    "number",
)
ROUNDING_NOTE = (
    "Amounts are calculated exactly and rounded once for display (half-up to Rs 0.01); a total "
    "can therefore differ from the sum of its displayed lines by Rs 0.01."
)


@dataclass
class Column:
    label: str
    kind: str = TEXT


@dataclass
class Section:
    title: str
    columns: list
    rows: list = field(default_factory=list)
    total_row: list | None = None  # same width as columns, exact values
    note: str = ""

    def with_totals(self, *money_columns):
        """Add a total row summing the given column indexes exactly."""
        if not self.rows:
            return self
        total = [None] * len(self.columns)
        total[0] = "Total"
        for i in money_columns:
            total[i] = sum((Fraction(r[i] or 0) for r in self.rows), Fraction(0))
        self.total_row = total
        return self


@dataclass
class Report:
    key: str
    title: str
    subtitle: str = ""
    sections: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def row_count(self):
        return sum(len(s.rows) for s in self.sections)
