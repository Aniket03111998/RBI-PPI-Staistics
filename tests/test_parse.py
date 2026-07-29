"""Golden tests pinned to the Phase 0 sample files (data/raw/*.xlsx).

These values were read directly from the raw XLSX cells (not derived from parse.py
itself), so a parsing regression has to actually disagree with the source file to fail
this. Run: python -m unittest tests.test_parse -v
"""
from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from ppi.parse import parse_file

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def metric_value(df, entity, metric, instrument):
    row = df[(df.entity_raw == entity) & (df.metric == metric) & (df.instrument == instrument)]
    return row["value"].iloc[0] if not row.empty else None


class TestParseJune2026(unittest.TestCase):
    """16-column schema (post active_count addition). Row 8 = A U Small Finance Bank Limited."""

    @classmethod
    def setUpClass(cls):
        cls.df, cls.unmatched = parse_file(RAW_DIR / "ppi_2026-06.xlsx", date(2026, 6, 1), False)

    def test_bank_row_values(self):
        e = "A U Small Finance Bank Limited"
        self.assertEqual(metric_value(self.df, e, "outstanding_count", "card"), 2130119)
        self.assertEqual(metric_value(self.df, e, "outstanding_count", "wallet"), 64054)
        self.assertEqual(metric_value(self.df, e, "active_count", "card"), 2128895)
        self.assertEqual(metric_value(self.df, e, "active_count", "wallet"), 34711)
        self.assertEqual(metric_value(self.df, e, "purchase_txn_volume", "card"), 20695)
        self.assertAlmostEqual(metric_value(self.df, e, "purchase_txn_value", "card"), 610.65184)
        self.assertEqual(metric_value(self.df, e, "purchase_txn_volume", "wallet"), 107485)
        self.assertAlmostEqual(metric_value(self.df, e, "purchase_txn_value", "wallet"), 10236.66212)
        self.assertEqual(metric_value(self.df, e, "fund_transfer_txn_volume", "card"), 0)
        self.assertEqual(metric_value(self.df, e, "cash_withdrawal_atm_txn_volume", "card"), 0)

    def test_entity_type_split(self):
        e_bank = self.df[self.df.entity_raw == "A U Small Finance Bank Limited"]
        e_nonbank = self.df[self.df.entity_raw == "Aditya Birla Capital Digital Limited"]
        self.assertEqual(set(e_bank.entity_type), {"bank"})
        self.assertEqual(set(e_nonbank.entity_type), {"non_bank"})

    def test_first_nonbank_row_values(self):
        e = "Aditya Birla Capital Digital Limited"
        self.assertEqual(metric_value(self.df, e, "outstanding_count", "card"), 59412)
        self.assertEqual(metric_value(self.df, e, "outstanding_count", "wallet"), 3440)
        self.assertEqual(metric_value(self.df, e, "purchase_txn_volume", "card"), 23281)
        self.assertAlmostEqual(metric_value(self.df, e, "purchase_txn_value", "card"), 106286.57786)

    def test_total_row_excluded(self):
        self.assertNotIn("Total", set(self.df.entity_raw))
        self.assertFalse(self.df.entity_raw.str.strip().str.lower().eq("total").any())

    def test_bank_and_nonbank_counts(self):
        self.assertEqual(self.df[self.df.entity_type == "bank"]["entity_raw"].nunique(), 38)
        self.assertEqual(self.df[self.df.entity_type == "non_bank"]["entity_raw"].nunique(), 48)


class TestParseJuly2025SchemaVariant(unittest.TestCase):
    """14-column schema (no active_count) — must not silently invent the metric."""

    @classmethod
    def setUpClass(cls):
        cls.df, cls.unmatched = parse_file(RAW_DIR / "ppi_2025-07.xlsx", date(2025, 7, 1), False)

    def test_bank_row_values(self):
        e = "A U Small Finance Bank Limited"
        self.assertEqual(metric_value(self.df, e, "outstanding_count", "card"), 1846984)
        self.assertEqual(metric_value(self.df, e, "outstanding_count", "wallet"), 65629)
        self.assertEqual(metric_value(self.df, e, "purchase_txn_volume", "card"), 8230)
        self.assertAlmostEqual(metric_value(self.df, e, "purchase_txn_value", "card"), 287.611)
        self.assertEqual(metric_value(self.df, e, "purchase_txn_volume", "wallet"), 163887)
        self.assertAlmostEqual(metric_value(self.df, e, "purchase_txn_value", "wallet"), 15366.133)

    def test_active_count_absent_not_zero(self):
        """This schema variant has no Active Instruments columns at all — active_count
        must be genuinely absent from the output, not a fabricated 0."""
        self.assertTrue((self.df["metric"] != "active_count").all())


if __name__ == "__main__":
    unittest.main()
