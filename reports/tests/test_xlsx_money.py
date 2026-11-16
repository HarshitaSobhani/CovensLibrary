"""Money at the spreadsheet boundary. The database and every calculation stay in integer paise;
XLSX cells receive rupees rounded half-up to 0.01 (a display boundary, as for PDF and CSV)."""

from decimal import Decimal
from fractions import Fraction

from django.test import SimpleTestCase

from reports import render
from reports.core import MONEY


class XlsxMoneyBoundaryTests(SimpleTestCase):
    def cell(self, paise):
        return render._xlsx_value(paise, MONEY)

    def test_whole_paise_convert_exactly_to_rupees(self):
        for paise in (0, 1, 9, 10, 99, 100, 101, 1050, 99999, 4999999):
            value = self.cell(paise)
            self.assertIsInstance(value, float)
            self.assertEqual(Decimal(str(value)) * 100, Decimal(paise), paise)

    def test_the_largest_permitted_amount_is_exact(self):
        # Rs 49,999.99: the largest amount the application accepts (incl. unit cost)
        self.assertEqual(self.cell(4999999), 49999.99)
        self.assertEqual(Decimal(str(self.cell(4999999))), Decimal("49999.99"))

    def test_fractional_paise_from_allocation_round_half_up_to_the_paisa(self):
        self.assertEqual(self.cell(Fraction(1, 2)), 0.01)  # half a paisa rounds up
        self.assertEqual(self.cell(Fraction(49, 100)), 0.0)  # below half: down
        self.assertEqual(self.cell(Fraction(3, 2)), 0.02)
        self.assertEqual(self.cell(Fraction(1049, 2)), 5.25)  # 524.5 paise -> 525 paise

    def test_rounding_is_display_only_the_stored_paise_are_unchanged(self):
        stored = 1234  # what the database holds; the spreadsheet value is derived, never stored
        derived = render.to_decimal(Fraction(stored) / 100)
        self.assertEqual(derived, Decimal("12.34"))
        self.assertEqual(stored, 1234)

    def test_money_format_is_two_decimals_and_nothing_is_negative_by_accident(self):
        self.assertEqual(self.cell(0), 0.0)
        self.assertEqual(self.cell(None), None)
        self.assertEqual(self.cell(""), None)
